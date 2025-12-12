"""
Authentication service for AI Hub
"""

import os
import sys
import shutil
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
from app.core.oauth import direct_oauth

logger = logging.getLogger(__name__)


def find_claude_executable() -> Optional[str]:
    """Find the claude executable, handling Windows/npm installations"""
    # First try shutil.which (works for PATH)
    claude_path = shutil.which('claude')
    if claude_path:
        return claude_path

    # On Windows, check common npm installation paths
    if sys.platform == 'win32':
        possible_paths = [
            Path(os.environ.get('APPDATA', '')) / 'npm' / 'claude.cmd',
            Path(os.environ.get('APPDATA', '')) / 'npm' / 'claude',
            Path.home() / 'AppData' / 'Roaming' / 'npm' / 'claude.cmd',
        ]
        for p in possible_paths:
            if p.exists():
                return str(p)

    return None


def find_gh_executable() -> Optional[str]:
    """Find the gh executable, handling Windows installations"""
    # First try shutil.which (works for PATH)
    gh_path = shutil.which('gh')
    if gh_path:
        return gh_path

    # On Windows, check common installation paths
    if sys.platform == 'win32':
        possible_paths = [
            Path(os.environ.get('ProgramFiles', '')) / 'GitHub CLI' / 'gh.exe',
            Path(os.environ.get('LOCALAPPDATA', '')) / 'Programs' / 'GitHub CLI' / 'gh.exe',
            Path.home() / 'scoop' / 'apps' / 'gh' / 'current' / 'gh.exe',
        ]
        for p in possible_paths:
            if p.exists():
                return str(p)

    return None


def run_subprocess_cmd(cmd: list, **kwargs) -> subprocess.CompletedProcess:
    """
    Run a subprocess command with proper Windows handling.

    Security Note: This function should only be called with system-generated
    commands (paths from shutil.which, known CLI arguments). Never pass
    user-controlled input directly to this function.
    """
    # Security: Validate that command arguments don't contain shell metacharacters
    # This is a defense-in-depth measure - callers should already sanitize input
    shell_metacharacters = set(';&|$`\\"\'\n\r')
    for arg in cmd:
        if any(c in arg for c in shell_metacharacters):
            raise ValueError(f"Command argument contains potentially dangerous characters: {arg[:50]}")

    if sys.platform == 'win32' and cmd and cmd[0].endswith('.cmd'):
        # On Windows, .cmd files need shell=True
        return subprocess.run(' '.join(f'"{c}"' if ' ' in c else c for c in cmd), shell=True, **kwargs)
    return subprocess.run(cmd, **kwargs)


class AuthService:
    """Handles both web UI authentication, Claude CLI, and GitHub CLI authentication"""

    def __init__(self):
        """Initialize auth service"""
        # Use HOME environment variable for config directories
        # On Windows, fall back to USERPROFILE if HOME is not set
        home = os.environ.get('HOME') or os.environ.get('USERPROFILE')
        if home:
            home = Path(home)
        else:
            # Last resort fallback
            home = Path.home() if sys.platform == 'win32' else Path('/home/appuser')
        self.config_dir = home / '.claude'
        self.gh_config_dir = home / '.config' / 'gh'

        # Note: OAuth state is now managed by direct_oauth module (app/core/oauth.py)

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

    def verify_password(self, password: str, password_hash: str) -> bool:
        """Verify a password against a hash"""
        try:
            return bcrypt.checkpw(
                password.encode('utf-8'),
                password_hash.encode('utf-8')
            )
        except Exception:
            return False

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
        # Ensure onboarding is marked complete when credentials exist
        self._ensure_onboarding_complete()
        return True

    def validate_claude_credentials(self) -> Dict[str, Any]:
        """
        Validate Claude credentials by running a simple CLI command.
        This checks if the OAuth token is still valid (not just if the file exists).

        Returns:
            Dict with 'valid' boolean and 'error' message if invalid.
        """
        creds_file = self.config_dir / '.credentials.json'

        # First check if credentials file exists
        if not creds_file.exists() or creds_file.stat().st_size == 0:
            return {
                "valid": False,
                "authenticated": False,
                "error": "No credentials file found"
            }

        try:
            home_env = os.environ.get('HOME', str(Path.home()))

            # Find claude executable
            claude_cmd = find_claude_executable()
            if not claude_cmd:
                return {
                    "valid": False,
                    "authenticated": True,  # File exists but can't validate
                    "error": "Claude CLI not found"
                }

            use_shell = sys.platform == 'win32' and claude_cmd.endswith('.cmd')

            # Run 'claude --version' or a simple non-interactive command
            # to check if credentials are valid
            result = subprocess.run(
                [claude_cmd, '--version'] if not use_shell else f'"{claude_cmd}" --version',
                capture_output=True,
                text=True,
                timeout=10,
                shell=use_shell,
                env={**os.environ, 'HOME': home_env}
            )

            # If this works, credentials are likely valid
            # Note: --version doesn't actually validate OAuth, but if the CLI
            # is configured and works, that's a good sign
            if result.returncode == 0:
                return {
                    "valid": True,
                    "authenticated": True,
                    "version": result.stdout.strip() if result.stdout else None
                }
            else:
                # Check if error indicates auth issue
                error_output = result.stderr.lower() if result.stderr else ''
                if 'unauthorized' in error_output or 'auth' in error_output or 'expired' in error_output:
                    return {
                        "valid": False,
                        "authenticated": True,  # File exists but token expired
                        "error": "Credentials may be expired",
                        "details": result.stderr
                    }
                return {
                    "valid": True,  # Assume valid if not auth error
                    "authenticated": True
                }

        except subprocess.TimeoutExpired:
            return {
                "valid": False,
                "authenticated": True,
                "error": "CLI command timed out"
            }
        except Exception as e:
            logger.error(f"Error validating Claude credentials: {e}")
            return {
                "valid": False,
                "authenticated": True,
                "error": str(e)
            }

    def _ensure_onboarding_complete(self):
        """
        Ensure settings.json has hasCompletedOnboarding=true.
        This prevents the CLI from showing the onboarding wizard when
        spawning interactive terminals (like /rewind).
        """
        settings_file = self.config_dir / 'settings.json'

        try:
            # Read existing settings or start with empty dict
            if settings_file.exists():
                with open(settings_file, 'r') as f:
                    settings_data = json.load(f)
            else:
                settings_data = {}

            # Only update if not already set
            if not settings_data.get('hasCompletedOnboarding'):
                settings_data['hasCompletedOnboarding'] = True

                # Set default theme if not present
                if 'theme' not in settings_data:
                    settings_data['theme'] = 'dark'

                # Write back
                with open(settings_file, 'w') as f:
                    json.dump(settings_data, f, indent=2)

                logger.info("Set hasCompletedOnboarding=true in settings.json")
        except Exception as e:
            logger.warning(f"Could not update settings.json: {e}")

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
        creds_file = self.config_dir / '.credentials.json'
        cli_success = False
        cli_error = None

        try:
            home_env = os.environ.get('HOME', str(Path.home()))

            # Find claude executable
            claude_cmd = find_claude_executable()
            if claude_cmd:
                use_shell = sys.platform == 'win32' and claude_cmd.endswith('.cmd')

                result = subprocess.run(
                    [claude_cmd, 'logout'] if not use_shell else f'"{claude_cmd}" logout',
                    capture_output=True,
                    text=True,
                    timeout=30,
                    shell=use_shell,
                    env={**os.environ, 'HOME': home_env}
                )

                if result.returncode == 0:
                    cli_success = True
                else:
                    cli_error = result.stderr
            else:
                cli_error = "Claude CLI not found"

        except Exception as e:
            logger.error(f"Claude logout CLI error: {e}")
            cli_error = str(e)

        # Fallback: directly delete credentials file if it still exists
        file_deleted = False
        if creds_file.exists():
            try:
                creds_file.unlink()
                file_deleted = True
                logger.info(f"Deleted credentials file: {creds_file}")
            except Exception as e:
                logger.error(f"Failed to delete credentials file: {e}")
                if not cli_success:
                    return {
                        "success": False,
                        "message": "Logout failed",
                        "error": f"CLI error: {cli_error}. File deletion error: {e}"
                    }

        # Success if either CLI succeeded or file was deleted/doesn't exist
        if cli_success or file_deleted or not creds_file.exists():
            return {
                "success": True,
                "message": "Logged out from Claude Code"
            }
        else:
            return {
                "success": False,
                "message": "Logout failed",
                "error": cli_error or "Unknown error"
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
            gh_cmd = find_gh_executable()
            if not gh_cmd:
                return False

            home_env = os.environ.get('HOME', str(Path.home()))
            result = subprocess.run(
                [gh_cmd, 'auth', 'status'],
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
                gh_cmd = find_gh_executable()
                if gh_cmd:
                    home_env = os.environ.get('HOME', str(Path.home()))
                    result = subprocess.run(
                        [gh_cmd, 'api', 'user', '-q', '.login'],
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
            gh_cmd = find_gh_executable()
            if not gh_cmd:
                return {
                    "success": False,
                    "message": "GitHub CLI not found",
                    "error": "Could not find 'gh' command. Please install GitHub CLI."
                }

            home_env = os.environ.get('HOME', str(Path.home()))

            # Ensure config directory exists
            self.gh_config_dir.mkdir(parents=True, exist_ok=True)

            # Login with the token
            result = subprocess.run(
                [gh_cmd, 'auth', 'login', '--with-token'],
                input=token,
                capture_output=True,
                text=True,
                timeout=30,
                env={**os.environ, 'HOME': home_env}
            )

            if result.returncode == 0:
                # Configure git credential helper
                git_cmd = shutil.which('git')
                if git_cmd:
                    subprocess.run(
                        [git_cmd, 'config', '--global', 'credential.helper', '!gh auth git-credential'],
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
            gh_cmd = find_gh_executable()
            if not gh_cmd:
                # Just remove the config file if gh not found
                hosts_file = self.gh_config_dir / 'hosts.yml'
                if hosts_file.exists():
                    hosts_file.unlink()
                return {
                    "success": True,
                    "message": "Logged out from GitHub (config removed)"
                }

            home_env = os.environ.get('HOME', str(Path.home()))
            result = subprocess.run(
                [gh_cmd, 'auth', 'logout', '--hostname', 'github.com'],
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
    # Claude Code OAuth Login (Direct OAuth - no PTY/CLI interaction needed)
    # =========================================================================

    def start_claude_oauth_login(self, force_reauth: bool = False) -> Dict[str, Any]:
        """
        Start Claude Code OAuth login using direct OAuth 2.0 + PKCE flow.

        This directly implements the OAuth flow against Anthropic's endpoints,
        bypassing the need for CLI/PTY interaction. Much more reliable and
        works on all platforms.

        The flow:
        1. Generate PKCE code verifier and challenge
        2. Return OAuth URL for user to visit
        3. User authenticates and gets authorization code
        4. User calls complete_claude_oauth_login with code + state
        5. We exchange code for tokens and write credentials file

        Args:
            force_reauth: If True, delete existing credentials and force re-authentication.

        Returns:
            Dict with oauth_url and state on success, or error details on failure.
        """
        try:
            # If force_reauth, delete existing credentials first
            if force_reauth:
                creds_file = self.config_dir / '.credentials.json'
                if creds_file.exists():
                    try:
                        creds_file.unlink()
                        logger.info(f"Force reauth: deleted credentials file {creds_file}")
                    except Exception as e:
                        logger.warning(f"Failed to delete credentials for force reauth: {e}")

            # Check if already authenticated (skip if force_reauth)
            if not force_reauth and self.is_claude_authenticated():
                return {
                    "success": True,
                    "already_authenticated": True,
                    "message": "Already authenticated with Claude Code"
                }

            # Start the direct OAuth flow
            result = direct_oauth.start_oauth_flow()

            logger.info(f"Started direct OAuth flow, state: {result['state'][:8]}...")

            return {
                "success": True,
                "oauth_url": result["oauth_url"],
                "state": result["state"],
                "message": "Open this URL in your browser to authenticate with Claude.",
                "requires_code": True,
            }

        except Exception as e:
            logger.error(f"Claude OAuth login error: {e}", exc_info=True)
            return {
                "success": False,
                "message": "Login failed",
                "error": str(e)
            }

    async def complete_claude_oauth_login(self, auth_code: str, state: str) -> Dict[str, Any]:
        """
        Complete the Claude OAuth login by exchanging the authorization code for tokens.

        This is the second step of the direct OAuth flow. After the user visits
        the OAuth URL and authenticates, they receive an authorization code which
        is exchanged for access/refresh tokens.

        Args:
            auth_code: The authorization code from the OAuth callback
            state: The state parameter from the original OAuth request (for PKCE validation)

        Returns:
            Dict with success status and message.
        """
        try:
            # Complete the OAuth flow (exchanges code, writes credentials)
            result = await direct_oauth.complete_oauth_flow(auth_code, state)

            if result["success"]:
                # Also ensure onboarding is marked complete
                self._ensure_onboarding_complete()
                logger.info("Direct OAuth login completed successfully")

            return result

        except Exception as e:
            logger.error(f"Error completing Claude OAuth login: {e}", exc_info=True)
            return {
                "success": False,
                "message": "Failed to complete login",
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
