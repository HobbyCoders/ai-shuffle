"""
Authentication helper for Claude Code OAuth flow
Manages login state and authentication tokens
"""

import os
import json
import asyncio
import subprocess
from pathlib import Path
from typing import Optional, Dict, Any
import logging

logger = logging.getLogger(__name__)


class ClaudeAuthHelper:
    """Helper class to manage Claude Code authentication"""

    def __init__(self, config_dir: Optional[str] = None):
        """
        Initialize authentication helper

        Args:
            config_dir: Directory to store Claude config (defaults to ~/.config/claude)
        """
        if config_dir:
            self.config_dir = Path(config_dir)
        else:
            home = os.environ.get('HOME', '/home/appuser')
            self.config_dir = Path(home) / '.config' / 'claude'

        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.auth_file = self.config_dir / 'auth.json'

    def is_authenticated(self) -> bool:
        """
        Check if user is authenticated with Claude Code

        Returns:
            bool: True if authenticated, False otherwise
        """
        try:
            # Check for credentials file in ~/.claude/ directory
            # Claude Code 2.x stores credentials in ~/.claude/.credentials.json
            home = os.environ.get('HOME', '/home/appuser')
            claude_dir = Path(home) / '.claude'
            credentials_file = claude_dir / '.credentials.json'

            authenticated = credentials_file.exists() and credentials_file.stat().st_size > 0

            logger.info(f"Auth check - credentials file: {credentials_file}")
            logger.info(f"Auth check - exists: {credentials_file.exists()}")
            logger.info(f"Auth check - authenticated: {authenticated}")
            logger.info(f"HOME env var: {home}")
            logger.info(f"Config dir: {self.config_dir}")

            return authenticated
        except Exception as e:
            logger.error(f"Error checking auth status: {e}")
            return False

    async def get_login_instructions(self) -> Dict[str, str]:
        """
        Get instructions for logging in to Claude Code

        Returns:
            Dict with login instructions and status
        """
        if self.is_authenticated():
            return {
                "status": "authenticated",
                "message": "Already authenticated with Claude Code",
                "action": "none"
            }

        return {
            "status": "not_authenticated",
            "message": "Not authenticated. Use the /login endpoint to authenticate.",
            "action": "login_required",
            "instructions": [
                "1. Access the container shell: docker exec -it claude-sdk-agent /bin/bash",
                "2. Run: claude login",
                "3. Follow the OAuth flow in your browser",
                "4. Return here and verify with /auth/status"
            ]
        }

    async def initiate_login(self) -> Dict[str, Any]:
        """
        Initiate the Claude Code login process

        Returns:
            Dict with login process information
        """
        try:
            # Start the login process
            process = subprocess.Popen(
                ['claude', 'login'],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )

            # Read initial output
            output_lines = []
            try:
                # Wait a bit for initial output
                await asyncio.sleep(2)

                # Try to read available output
                if process.poll() is None:  # Still running
                    # Process is waiting for user interaction
                    output_lines.append("Login process started. Please check container logs.")
                else:
                    stdout, stderr = process.communicate(timeout=1)
                    output_lines.extend(stdout.split('\n'))
                    if stderr:
                        output_lines.extend(stderr.split('\n'))

            except subprocess.TimeoutExpired:
                output_lines.append("Login process is running...")

            return {
                "status": "login_initiated",
                "message": "Claude Code login process started",
                "output": output_lines,
                "instructions": [
                    "The login process requires interactive input.",
                    "Please access the container directly:",
                    "  docker exec -it claude-sdk-agent /bin/bash",
                    "  claude login",
                    "Then follow the OAuth flow in your browser."
                ]
            }

        except Exception as e:
            logger.error(f"Error initiating login: {e}")
            return {
                "status": "error",
                "message": f"Failed to initiate login: {str(e)}",
                "instructions": [
                    "Please manually login using:",
                    "  docker exec -it claude-sdk-agent claude login"
                ]
            }

    async def logout(self) -> Dict[str, str]:
        """
        Logout from Claude Code

        Returns:
            Dict with logout status
        """
        try:
            result = subprocess.run(
                ['claude', 'logout'],
                capture_output=True,
                text=True,
                timeout=10
            )

            if result.returncode == 0:
                return {
                    "status": "success",
                    "message": "Successfully logged out from Claude Code"
                }
            else:
                return {
                    "status": "error",
                    "message": f"Logout failed: {result.stderr}"
                }

        except Exception as e:
            logger.error(f"Error during logout: {e}")
            return {
                "status": "error",
                "message": f"Logout failed: {str(e)}"
            }

    def get_auth_info(self) -> Dict[str, Any]:
        """
        Get current authentication information

        Returns:
            Dict with auth info
        """
        is_auth = self.is_authenticated()

        info = {
            "authenticated": is_auth,
            "config_dir": str(self.config_dir),
        }

        if is_auth:
            info["message"] = "Authenticated with Claude Code"
        else:
            info["message"] = "Not authenticated. Login required."

        return info
