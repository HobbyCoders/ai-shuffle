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
    """Run a subprocess command with proper Windows handling"""
    if sys.platform == 'win32' and cmd and cmd[0].endswith('.cmd'):
        # On Windows, .cmd files need shell=True
        return subprocess.run(' '.join(f'"{c}"' if ' ' in c else c for c in cmd), shell=True, **kwargs)
    return subprocess.run(cmd, **kwargs)


class AuthService:
    """Handles both web UI authentication, Claude CLI, and GitHub CLI authentication"""

    def __init__(self):
        """Initialize auth service"""
        self.config_dir = Path(os.environ.get('HOME', '/home/appuser')) / '.claude'
        self.gh_config_dir = Path(os.environ.get('HOME', '/home/appuser')) / '.config' / 'gh'
        # Store active OAuth login process for multi-step flow
        self._claude_login_process = None
        self._claude_login_master_fd = None

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
            home_env = os.environ.get('HOME', str(Path.home()))

            # Find claude executable
            claude_cmd = find_claude_executable()
            if not claude_cmd:
                return {
                    "success": False,
                    "message": "Claude CLI not found",
                    "error": "Could not find 'claude' command."
                }

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
    # Claude Code OAuth Login (in-app)
    # =========================================================================

    def _cleanup_claude_login_process(self):
        """Clean up any existing claude login process"""
        if self._claude_login_process:
            try:
                self._claude_login_process.kill()
                self._claude_login_process.wait(timeout=2)
            except:
                pass
            self._claude_login_process = None

        if self._claude_login_master_fd:
            try:
                import os as os_module
                os_module.close(self._claude_login_master_fd)
            except:
                pass
            self._claude_login_master_fd = None

    def _read_pty_output(self, timeout: float = 5.0) -> str:
        """Read available output from the PTY master fd"""
        import select
        import os as os_module
        import time

        output = ""
        start = time.time()

        while time.time() - start < timeout:
            if not self._claude_login_master_fd:
                break

            ready, _, _ = select.select([self._claude_login_master_fd], [], [], 0.3)
            if ready:
                try:
                    data = os_module.read(self._claude_login_master_fd, 4096)
                    if data:
                        chunk = data.decode('utf-8', errors='replace')
                        output += chunk
                        logger.debug(f"PTY read: {repr(chunk)}")
                except OSError:
                    break
            else:
                # No more data available right now
                if output:
                    break

        return output

    def _write_pty_input(self, text: str):
        """Write input to the PTY master fd"""
        import os as os_module

        if self._claude_login_master_fd:
            try:
                os_module.write(self._claude_login_master_fd, text.encode('utf-8'))
                logger.debug(f"PTY write: {repr(text)}")
            except OSError as e:
                logger.error(f"Failed to write to PTY: {e}")

    def start_claude_oauth_login(self) -> Dict[str, Any]:
        """
        Start Claude Code OAuth login process.

        The claude CLI login flow is interactive with multiple steps:
        1. Theme selection - we send "1"
        2. Login method selection - we send "1" (browser-based OAuth)
        3. CLI displays an OAuth URL (browser fails to open in Docker)
        4. User opens URL in browser and authenticates with Anthropic
        5. User copies the resulting code
        6. User calls /auth/claude/complete with the code
        7. We send the code to the CLI process
        8. CLI shows confirmation - we send Enter
        9. Security notes - we send Enter
        10. Folder permission (/app) - we send "1"
        11. CLI creates ~/.claude/.credentials.json

        For Docker: We run this in the container and manage the process
        For Windows/native: User should run 'claude' in their terminal
        """
        try:
            # Check if already authenticated
            if self.is_claude_authenticated():
                return {
                    "success": True,
                    "already_authenticated": True,
                    "message": "Already authenticated with Claude Code"
                }

            # Clean up any existing login process
            self._cleanup_claude_login_process()

            home_env = os.environ.get('HOME', str(Path.home()))

            # Ensure config directory exists
            self.config_dir.mkdir(parents=True, exist_ok=True)

            # Find claude executable
            claude_cmd = find_claude_executable()
            if not claude_cmd:
                return {
                    "success": False,
                    "message": "Claude CLI not found",
                    "error": "Could not find 'claude' command. Please ensure Claude Code CLI is installed."
                }

            # Check if we're in Docker (typical indicators)
            in_docker = (
                os.path.exists('/.dockerenv') or
                os.environ.get('DOCKER_CONTAINER') == 'true' or
                os.path.exists('/app/main.py')  # Our Docker layout
            )

            if in_docker:
                import re
                import time
                import pty
                import os as os_module

                try:
                    # Create PTY for interactive communication
                    master_fd, slave_fd = pty.openpty()

                    # Start claude (not 'claude login' - just 'claude' will prompt for login if needed)
                    process = subprocess.Popen(
                        [claude_cmd],
                        stdin=slave_fd,
                        stdout=slave_fd,
                        stderr=slave_fd,
                        close_fds=True,
                        env={**os.environ, 'HOME': home_env, 'TERM': 'xterm'}
                    )

                    os_module.close(slave_fd)

                    # Store references for later use
                    self._claude_login_process = process
                    self._claude_login_master_fd = master_fd

                    # Wait a moment for initial output
                    time.sleep(0.5)

                    # Read initial output (theme selection)
                    output = self._read_pty_output(timeout=3.0)
                    logger.info(f"Initial claude output: {repr(output[:500] if len(output) > 500 else output)}")

                    # Step 1: Theme selection - send "1" for default theme
                    # Look for theme selection prompt
                    if 'theme' in output.lower() or 'select' in output.lower() or '1.' in output:
                        logger.info("Sending theme selection: 1")
                        self._write_pty_input("1\n")
                        time.sleep(0.5)
                        output = self._read_pty_output(timeout=3.0)
                        logger.info(f"After theme selection: {repr(output[:500] if len(output) > 500 else output)}")

                    # Step 2: Login method selection - send "1" for browser OAuth
                    if 'login' in output.lower() or 'method' in output.lower() or '1.' in output:
                        logger.info("Sending login method selection: 1")
                        self._write_pty_input("1\n")
                        time.sleep(1.0)  # Give more time for URL to appear
                        output = self._read_pty_output(timeout=5.0)
                        logger.info(f"After login method: {repr(output[:500] if len(output) > 500 else output)}")

                    # Step 3: Extract the OAuth URL
                    # The URL should be displayed after browser fails to open
                    all_output = output
                    url_match = re.search(r'(https://[^\s\x00-\x1f\]]+)', all_output)

                    if url_match:
                        oauth_url = url_match.group(1).rstrip(')')
                        logger.info(f"Extracted OAuth URL: {oauth_url}")
                        return {
                            "success": True,
                            "oauth_url": oauth_url,
                            "message": "Open this URL in your browser, authenticate, then copy the code and use /auth/claude/complete to finish.",
                            "requires_code": True,
                            "process_active": True
                        }
                    else:
                        # Try reading more output in case URL is delayed
                        time.sleep(2.0)
                        more_output = self._read_pty_output(timeout=5.0)
                        all_output += more_output
                        logger.info(f"Additional output: {repr(more_output[:500] if len(more_output) > 500 else more_output)}")

                        url_match = re.search(r'(https://[^\s\x00-\x1f\]]+)', all_output)
                        if url_match:
                            oauth_url = url_match.group(1).rstrip(')')
                            logger.info(f"Extracted OAuth URL (delayed): {oauth_url}")
                            return {
                                "success": True,
                                "oauth_url": oauth_url,
                                "message": "Open this URL in your browser, authenticate, then copy the code and use /auth/claude/complete to finish.",
                                "requires_code": True,
                                "process_active": True
                            }

                        # Still no URL - clean up and report error
                        self._cleanup_claude_login_process()
                        logger.warning(f"Could not find URL in claude output: {repr(all_output)}")
                        return {
                            "success": False,
                            "message": "Could not extract OAuth URL",
                            "error": all_output or "No URL found in claude output",
                            "raw_output": all_output,
                            "instructions": "Run 'claude' manually in the container terminal: docker exec -it <container> claude"
                        }

                except (ImportError, OSError) as e:
                    self._cleanup_claude_login_process()
                    logger.error(f"PTY error: {e}")
                    return {
                        "success": False,
                        "message": "Failed to start interactive login",
                        "error": str(e),
                        "instructions": "Run 'claude' manually in the container terminal"
                    }

            else:
                # Not in Docker - provide instructions for manual login
                return {
                    "success": False,
                    "message": "Manual login required",
                    "error": "In-app OAuth login is only available in Docker deployments",
                    "instructions": [
                        "1. Open a terminal/command prompt",
                        "2. Run: claude",
                        "3. Follow the prompts (select theme, login method)",
                        "4. Click the URL that appears",
                        "5. Complete authentication in your browser",
                        "6. Copy the code and paste it back in the terminal",
                        "7. Refresh this page to verify authentication"
                    ]
                }

        except Exception as e:
            self._cleanup_claude_login_process()
            logger.error(f"Claude OAuth login error: {e}")
            return {
                "success": False,
                "message": "Login failed",
                "error": str(e)
            }

    def complete_claude_oauth_login(self, auth_code: str) -> Dict[str, Any]:
        """
        Complete the Claude OAuth login by sending the auth code to the waiting process.

        After the user visits the OAuth URL and gets a code, they call this endpoint
        to complete the authentication flow.
        """
        import time
        import re

        if not self._claude_login_process or not self._claude_login_master_fd:
            return {
                "success": False,
                "message": "No active login process",
                "error": "Please start the login process first with /auth/claude/login"
            }

        # Check if process is still running
        if self._claude_login_process.poll() is not None:
            self._cleanup_claude_login_process()
            return {
                "success": False,
                "message": "Login process has ended",
                "error": "The login process is no longer running. Please start again."
            }

        try:
            # Send the auth code
            logger.info(f"Sending auth code: {auth_code[:10]}...")
            self._write_pty_input(f"{auth_code}\n")
            time.sleep(1.0)

            # Read response
            output = self._read_pty_output(timeout=3.0)
            logger.info(f"After auth code: {repr(output[:500] if len(output) > 500 else output)}")

            # Step 5: Confirmation prompt - send Enter
            if output:
                logger.info("Sending confirmation Enter")
                self._write_pty_input("\n")
                time.sleep(0.5)
                output = self._read_pty_output(timeout=2.0)
                logger.info(f"After confirmation: {repr(output[:300] if len(output) > 300 else output)}")

            # Step 6: Security notes - send Enter
            if output:
                logger.info("Sending security notes Enter")
                self._write_pty_input("\n")
                time.sleep(0.5)
                output = self._read_pty_output(timeout=2.0)
                logger.info(f"After security notes: {repr(output[:300] if len(output) > 300 else output)}")

            # Step 7: Folder permission (/app) - send "1" for yes
            if 'folder' in output.lower() or 'permission' in output.lower() or '/app' in output or '1.' in output:
                logger.info("Sending folder permission: 1")
                self._write_pty_input("1\n")
                time.sleep(1.0)
                output = self._read_pty_output(timeout=3.0)
                logger.info(f"After folder permission: {repr(output[:300] if len(output) > 300 else output)}")

            # Give time for credentials to be written
            time.sleep(1.0)

            # Clean up the process
            self._cleanup_claude_login_process()

            # Check if authentication succeeded
            if self.is_claude_authenticated():
                return {
                    "success": True,
                    "message": "Successfully authenticated with Claude Code",
                    "authenticated": True
                }
            else:
                return {
                    "success": False,
                    "message": "Authentication may have failed",
                    "error": "Credentials file not found after login process. Check the output for errors.",
                    "last_output": output
                }

        except Exception as e:
            self._cleanup_claude_login_process()
            logger.error(f"Error completing Claude login: {e}")
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
