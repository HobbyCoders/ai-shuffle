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
import time
import asyncio
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional

import bcrypt

from app.db import database
from app.core.config import settings
from app.core.oauth import direct_oauth

logger = logging.getLogger(__name__)

# Import server instance ID from auth API for consistent logging
# Will be set by auth.py module initialization
_SERVER_INSTANCE_ID = "unknown"


def set_server_instance_id(instance_id: str):
    """Set the server instance ID for diagnostic logging"""
    global _SERVER_INSTANCE_ID
    _SERVER_INSTANCE_ID = instance_id


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

        # Match gh's config resolution, including custom/container locations.
        if os.environ.get('GH_CONFIG_DIR'):
            self.gh_config_dir = Path(os.environ['GH_CONFIG_DIR'])
        elif os.environ.get('XDG_CONFIG_HOME'):
            self.gh_config_dir = Path(os.environ['XDG_CONFIG_HOME']) / 'gh'
        elif sys.platform == 'win32':
            self.gh_config_dir = Path(os.environ.get('APPDATA', home / 'AppData' / 'Roaming')) / 'GitHub CLI'
        else:
            self.gh_config_dir = home / '.config' / 'gh'

        # Background process for GitHub web login device flow
        self._gh_web_login_process: Optional[subprocess.Popen] = None
        self._gh_web_login_error: Optional[str] = None

        # Note: OAuth state is now managed by direct_oauth module (app/core/oauth.py)

    # =========================================================================
    # Web UI Authentication
    # =========================================================================

    def is_setup_required(self) -> bool:
        from app.core.owner_auth import setup_required
        return setup_required()

    def setup_admin(self, username, password):
        raise ValueError('Worker passwords are retired; configure the local installation instead.')

    def login(self, username, password):
        return None

    def create_session(self):
        raise ValueError('Password sessions are retired.')

    def validate_session(self, token: str) -> bool:
        if not token or not token.startswith('shr_'):
            return False
        return database.get_auth_session(token) is not None

    def logout(self, token: str):
        if token:
            database.delete_auth_session(token)

    def get_admin_username(self) -> Optional[str]:
        from app.core.owner_auth import preference_identity
        return preference_identity()

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
        """Whether the stock CLI reports a login (``claude auth status``).

        Never reads the CLI's credential files; the answer is the CLI's own,
        briefly cached by ``claude_cli_status``.
        """
        from app.core import claude_cli_status
        return claude_cli_status.is_signed_in_cached()

    def validate_claude_credentials(self) -> Dict[str, Any]:
        """Ask the CLI again, bypassing the cache."""
        from app.core import claude_cli_status
        status = claude_cli_status.get_status(force=True)
        result: Dict[str, Any] = {
            "valid": bool(status.signed_in),
            "authenticated": bool(status.signed_in),
            "version": status.cli_version,
        }
        if status.error:
            result["error"] = status.error
        elif not status.signed_in:
            result["error"] = "The Claude Code CLI is not signed in"
        return result

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

    def get_claude_token_status(self) -> Dict[str, Any]:
        """Token-shaped view of the CLI probe, for callers of the old fields.

        The CLI owns and refreshes its token; AI Shuffle no longer reads it,
        so expiry and refresh-token presence are unknown.
        """
        signed_in = self.is_claude_authenticated()
        return {
            "token_valid": signed_in,
            "expired": False,
            "expires_at": None,
            "has_refresh_token": None,
        }

    def get_claude_auth_info(self) -> Dict[str, Any]:
        """Get Claude CLI authentication info, as the CLI reports it."""
        token_status = self.get_claude_token_status()
        return {
            "authenticated": token_status["token_valid"],
            "token_valid": token_status["token_valid"],
            "expired": False,
            "expires_at": None,
            "has_refresh_token": None,
            "config_dir": str(self.config_dir),
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
                "1. Sign in from Settings > Providers > Claude Code CLI, or open a terminal where AI Shuffle runs",
                "2. Run: claude auth login (Docker: docker exec -it -u appuser <container> claude auth login)",
                "3. Open the link it prints and sign in on Anthropic's page",
                "4. Return here and check again"
            ],
            "command": "claude auth login"
        }

    def claude_logout(self) -> Dict[str, Any]:
        """Sign the CLI out with its own command, `claude auth logout`.

        AI Shuffle never deletes or edits the CLI's credential files; if the
        CLI cannot sign out, the error is reported and nothing else happens.
        """
        from app.core import claude_cli_status
        try:
            status = claude_cli_status.get_status()
            claude_cmd = status.binary_path
            if not claude_cmd:
                return {"success": False, "message": "Logout failed",
                        "error": status.error or "Claude CLI not found"}
            env = {k: v for k, v in os.environ.items()
                   if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")}
            result = subprocess.run(
                [claude_cmd, 'auth', 'logout'],
                capture_output=True,
                text=True,
                timeout=30,
                stdin=subprocess.DEVNULL,
                env=env,
            )
        except Exception as e:
            logger.error(f"Claude logout CLI error: {e}")
            return {"success": False, "message": "Logout failed", "error": str(e)}
        finally:
            claude_cli_status.invalidate()

        if result.returncode != 0:
            return {
                "success": False,
                "message": "Logout failed",
                "error": (result.stderr or result.stdout or "claude auth logout failed").strip(),
            }
        # Clear the per-install active-account pointer so status reflects the
        # logout. Rows are intentionally KEPT — logout != forget accounts.
        try:
            from app.core import claude_accounts as _accounts
            _accounts._set_active_account_id(None)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Could not clear active Claude account on logout: {e}")
        return {
            "success": True,
            "message": "Logged out from Claude Code"
        }

    # =========================================================================
    # GitHub CLI Authentication
    # =========================================================================

    def is_github_authenticated(self) -> bool:
        """Check if GitHub CLI is authenticated"""
        # Primary check: run gh auth status (works for file and keyring auth)
        try:
            gh_cmd = find_gh_executable()
            if not gh_cmd:
                logger.debug("GitHub CLI not found")
                return False

            result = subprocess.run(
                [gh_cmd, 'auth', 'status', '--hostname', 'github.com'],
                capture_output=True,
                text=True,
                timeout=10,
                env=self._github_env()
            )
            if result.returncode == 0:
                return True

            # Log the error for debugging
            logger.debug(f"gh auth status returned non-zero: {result.stderr}")
        except subprocess.TimeoutExpired:
            logger.warning("GitHub auth status check timed out")
        except Exception as e:
            logger.warning(f"GitHub auth status check failed: {e}")

        return False

    def get_github_auth_info(self) -> Dict[str, Any]:
        """Get GitHub CLI authentication info and reconcile the device flow."""
        proc = self._gh_web_login_process
        if proc is not None and proc.poll() is not None:
            self._gh_web_login_process = None
            if proc.returncode == 0:
                try:
                    self._setup_github_git(find_gh_executable())
                except Exception:
                    self._gh_web_login_error = 'GitHub signed in, but Git credentials could not be configured. Retry connecting.'
            else:
                self._gh_web_login_error = 'GitHub web sign-in expired or was cancelled. Please try again.'
        authenticated = self.is_github_authenticated()
        user = None

        if authenticated:
            try:
                gh_cmd = find_gh_executable()
                if gh_cmd:
                    result = subprocess.run(
                        [gh_cmd, 'api', '--hostname', 'github.com', 'user', '-q', '.login'],
                        capture_output=True,
                        text=True,
                        timeout=10,
                        env=self._github_env()
                    )
                    if result.returncode == 0:
                        user = result.stdout.strip()
            except Exception as e:
                logger.warning(f"Could not get GitHub user: {e}")

        return {
            "authenticated": authenticated,
            "user": user,
            "config_dir": str(self.gh_config_dir),
            "web_login_pending": self._gh_web_login_process is not None,
            "web_login_error": self._gh_web_login_error,
        }

    def _github_env(self) -> Dict[str, str]:
        return {**os.environ, 'GH_CONFIG_DIR': str(self.gh_config_dir),
                'HOME': os.environ.get('HOME', str(Path.home()))}

    def _setup_github_git(self, gh_cmd: str) -> None:
        # Host-scoped helper; do not replace credential helpers for other hosts.
        result = subprocess.run(
            [gh_cmd, 'auth', 'setup-git', '--hostname', 'github.com'],
            capture_output=True, text=True, timeout=10, env=self._github_env(),
        )
        if result.returncode:
            raise RuntimeError('GitHub signed in, but Git credentials could not be configured. Retry connecting.')

    def _save_limited_github_token(self, token: str, user: str) -> None:
        """Save an API-validated, limited-scope PAT in gh's native file store.

        `gh auth login --with-token` insists on classic repo/read:org scopes,
        even for valid public-only PATs. Use its hosts format for that case,
        preserving other accounts/hosts and atomically writing owner-only data.
        """
        import tempfile
        import yaml

        hosts_file = self.gh_config_dir / 'hosts.yml'
        hosts = yaml.safe_load(hosts_file.read_text()) if hosts_file.exists() else {}
        if hosts is None:
            hosts = {}
        if not isinstance(hosts, dict):
            raise ValueError('GitHub CLI hosts configuration is invalid')
        host = hosts.setdefault('github.com', {})
        host.update(user=user, oauth_token=token, git_protocol='https')
        host.setdefault('users', {}).setdefault(user, {})['oauth_token'] = token
        fd, name = tempfile.mkstemp(prefix='.hosts-', dir=self.gh_config_dir)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                yaml.safe_dump(hosts, stream, sort_keys=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, hosts_file)
        finally:
            Path(name).unlink(missing_ok=True)

    def github_login_with_token(self, token: str) -> Dict[str, Any]:
        """Validate the PAT itself before saving it; permissions are per operation."""
        if not isinstance(token, str) or not token.strip():
            return {'success': False, 'error': 'A personal access token is required.'}
        token = token.strip()
        if any(c.isspace() for c in token):
            return {'success': False, 'error': 'The token must not contain whitespace.'}
        try:
            gh_cmd = find_gh_executable()
            if not gh_cmd:
                return {'success': False, 'error': 'GitHub CLI (gh) is not installed. Install it from https://cli.github.com/'}
            if os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN'):
                return {'success': False, 'error': 'GitHub is managed by GH_TOKEN/GITHUB_TOKEN in the server environment. Remove that setting before signing in here.'}
            self.cancel_github_web_login()
            env = self._github_env()
            # No token on the command line or in logs. This endpoint supports
            # classic and fine-grained PATs without requiring unrelated scopes.
            check = subprocess.run(
                [gh_cmd, 'api', '--hostname', 'github.com', 'user', '--jq', '.login'],
                capture_output=True, text=True, timeout=30,
                env={**env, 'GH_TOKEN': token},
            )
            if check.returncode or not check.stdout.strip():
                return {'success': False, 'error': 'GitHub could not validate this token. Check that it is correct, unexpired, and that github.com is reachable.'}
            user = check.stdout.strip()
            self.gh_config_dir.mkdir(parents=True, exist_ok=True)
            result = subprocess.run(
                [gh_cmd, 'auth', 'login', '--hostname', 'github.com',
                 '--git-protocol', 'https', '--with-token'],
                input=token, capture_output=True, text=True, timeout=30, env=env,
            )
            if result.returncode:
                error = (result.stderr or 'GitHub login failed').strip().replace(token, '[redacted]')
                if 'missing required scope' not in error:
                    return {'success': False, 'error': error}
                self._save_limited_github_token(token, user)
            # Read the persisted credential, not the validation override: a write
            # alone is not proof that future CLI and Git commands can use it.
            saved = subprocess.run(
                [gh_cmd, 'api', '--hostname', 'github.com', 'user', '--jq', '.login'],
                capture_output=True, text=True, timeout=30, env=env,
            )
            if saved.returncode or saved.stdout.strip() != user:
                return {'success': False, 'error': 'GitHub credentials were not saved correctly. Please retry.'}
            self._setup_github_git(gh_cmd)
            return {'success': True, 'message': 'Successfully logged in to GitHub', 'user': user}
        except subprocess.TimeoutExpired:
            return {'success': False, 'error': 'GitHub login timed out. Check your connection and retry.'}
        except Exception as exc:
            logger.warning('GitHub login failed (%s)', type(exc).__name__)
            return {'success': False, 'error': 'GitHub sign-in could not be saved or Git credentials could not be configured. Check GitHub CLI configuration and retry.'}

    def github_logout(self) -> Dict[str, Any]:
        """Logout from GitHub CLI"""
        self.cancel_github_web_login()
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

            result = subprocess.run(
                [gh_cmd, 'auth', 'logout', '--hostname', 'github.com'],
                input='Y\n',  # Confirm logout
                capture_output=True,
                text=True,
                timeout=30,
                env=self._github_env()
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
    # GitHub Device Flow (gh auth login --web)
    # =========================================================================

    def cancel_github_web_login(self) -> Dict[str, Any]:
        proc = self._gh_web_login_process
        self._gh_web_login_process = None
        self._gh_web_login_error = None
        if proc is not None:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)
        return {'success': True}

    async def github_login_web(self) -> Dict[str, Any]:
        """
        Start GitHub CLI device flow authentication (gh auth login --web).
        Runs gh in a background subprocess, parses the one-time code from output,
        and returns it to the caller. The frontend polls GET /auth/github/status
        for completion.
        """
        import re
        import threading

        await asyncio.to_thread(self.cancel_github_web_login)

        try:
            gh_cmd = find_gh_executable()
            if not gh_cmd:
                return {
                    "success": False,
                    "error": "GitHub CLI (gh) is not installed. Install it from https://cli.github.com/",
                    "install_url": "https://cli.github.com/"
                }

            self.gh_config_dir.mkdir(parents=True, exist_ok=True)

            if os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN'):
                return {'success': False, 'error': 'GitHub is managed by GH_TOKEN/GITHUB_TOKEN in the server environment. Remove that setting before signing in here.'}
            env = self._github_env()
            # Prevent gh from trying to open a browser automatically
            env['BROWSER'] = ''
            env['GH_BROWSER'] = ' '

            cmd = [gh_cmd, 'auth', 'login', '--hostname', 'github.com',
                   '--git-protocol', 'https', '--web']

            # On Windows, .cmd files need shell=True
            use_shell = sys.platform == 'win32' and gh_cmd.endswith('.cmd')
            if use_shell:
                cmd_str = ' '.join(f'"{c}"' if ' ' in c else c for c in cmd)
            else:
                cmd_str = cmd

            proc = subprocess.Popen(
                cmd_str,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=env,
                shell=use_shell,
            )
            self._gh_web_login_process = proc

            # Drain one merged stream for the entire process lifetime. Wake as
            # soon as the code arrives, without blocking the asyncio event loop.
            codes: list[str] = []
            ready = threading.Event()

            def read_output():
                try:
                    if proc.stdout is not None:
                        for line in proc.stdout:
                            match = re.search(r'\b([A-Z0-9]{4}-[A-Z0-9]{4})\b', line)
                            if match and not codes:
                                codes.append(match.group(1))
                                ready.set()
                finally:
                    ready.set()

            threading.Thread(target=read_output, daemon=True).start()
            await asyncio.to_thread(ready.wait, 15)
            if not codes:
                await asyncio.to_thread(self.cancel_github_web_login)
                return {'success': False, 'error': 'Could not start GitHub web sign-in. Check your connection or use a personal access token.'}
            user_code = codes[0]

            # Send Enter to stdin to proceed (gh waits for Enter before opening browser)
            try:
                if proc.stdin:
                    proc.stdin.write('\n')
                    proc.stdin.flush()
            except Exception:
                pass

            return {
                "success": True,
                "user_code": user_code,
                "verification_url": "https://github.com/login/device"
            }

        except Exception as e:
            logger.error(f"GitHub web login error: {e}")
            if self._gh_web_login_process is not None:
                try:
                    self._gh_web_login_process.kill()
                except Exception:
                    pass
                self._gh_web_login_process = None
            return {
                "success": False,
                "error": str(e)
            }

    # =========================================================================
    # Onboarding
    # =========================================================================

    def is_onboarding_complete(self) -> bool:
        """Check if the full onboarding flow has been completed for this instance.

        When multiple instances share a database, each local-mode instance needs
        its own workspace path configured (because each machine has a different
        home dir). Docker/non-local instances use a fixed default path, so no
        per-instance workspace is required.

        v2-projects semantics: the ONLY per-install requirement here is choosing
        this machine's workspace root — the directory under which project
        *checkouts* materialize (``checkouts.path``). Projects themselves are
        origin-keyed and install-independent (``projects_v2``), so they need NO
        per-install registration: a session/automation referencing an origin
        materializes its checkout lazily under this root. There is deliberately
        no project-level gate here (v2 projects are the unconditional model since
        PR-B) — the workspace-root pick is the whole gate.
        """
        val = database.get_system_setting("onboarding_complete")
        if val == "true":
            from app.core.config import settings
            from app.core.platform import is_local_mode as _is_local_mode

            # Docker/non-local instances use the fixed default workspace path
            # (e.g. /workspace), so they do not need a per-instance override.
            # Returning True here avoids an infinite setup redirect loop when
            # a second non-local instance connects to a shared database.
            if not _is_local_mode():
                return True

            # Local mode: each instance needs its own workspace path because
            # home directories differ per machine.
            instance_key = f"workspace_path:{settings.effective_instance_id}"
            if not database.get_system_setting(instance_key):
                # No instance-scoped workspace path. Check if there's a legacy
                # global one that matches this machine (backward compat).
                global_path = database.get_system_setting("workspace_path")
                if global_path:
                    import os
                    if os.path.exists(global_path):
                        # Legacy path exists on this machine — migrate it to instance-scoped
                        database.set_system_setting(instance_key, global_path)
                        return True
                # No workspace for this instance — force setup
                return False
            return True
        # If admin exists but the onboarding flow was never started via the
        # setup page, this is a user who created their admin before the setup
        # flow was introduced. Auto-mark onboarding as complete so they aren't
        # stuck on a setup screen that won't let them re-create an admin.
        if not self.is_setup_required():
            onboarding_started = database.get_system_setting("onboarding_started")
            if onboarding_started is None:
                logger.info("Admin exists but onboarding was never started — auto-completing onboarding for upgraded user")
                self.set_onboarding_complete()
                return True
        return False

    def set_onboarding_complete(self) -> None:
        """Mark the onboarding flow as complete"""
        database.set_system_setting("onboarding_complete", "true")
        logger.info("Onboarding marked as complete")

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
                        logger.debug(f"Force reauth: deleted credentials file {creds_file}")
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

            logger.debug(f"Started direct OAuth flow, state: {result['state'][:8]}...")

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
                logger.debug("Direct OAuth login completed successfully")
                # Land this fresh single-account login in the DB. If an active
                # account row already exists, the post-write hook has already
                # synced it; otherwise this seeds a default account. Never fatal.
                try:
                    from app.core.claude_accounts import seed_from_credentials_file
                    seed_from_credentials_file()
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"Claude account seed after login failed (ignored): {e}")

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
            "onboarding_complete": self.is_onboarding_complete(),
            "username": self.get_admin_username() if not setup_required else None
        }


# Global auth service instance
auth_service = AuthService()


# ─── Cached gh-connectivity signal ───────────────────────────────────────────
#
# ``is_github_authenticated()`` shells out to ``gh auth status`` with a 10s
# timeout. That is fine for the settings screen, which asks once, but the prompt
# builder asks on EVERY turn — an uncached subprocess there would put a process
# spawn (and, when gh is wedged, ten seconds) in front of each one. gh being
# signed in is a machine-level fact that changes rarely, so a short TTL is all
# the freshness the prompt needs: a fresh login shows up in the next section
# within five minutes, and a logout stops advertising just as quickly.
_GITHUB_AUTH_TTL_SECONDS = 300.0
# A *negative* answer is held far more briefly: it can also be the residue of a
# transient failure (gh timing out under load), and a sign-in performed outside
# the app should not stay invisible for five minutes.
_GITHUB_AUTH_NEGATIVE_TTL_SECONDS = 30.0
_github_auth_cache: Optional[tuple] = None  # (value, monotonic timestamp)


def is_github_authenticated_cached(probe: Optional[Callable[[], bool]] = None) -> bool:
    """``AuthService.is_github_authenticated()`` behind a TTL cache.

    Callers on a hot path (the per-turn prompt builder, ``GET /auth/status``)
    want the signal, not a subprocess. Errors are already swallowed by the
    underlying check, which returns False, so a cached False is a real answer
    rather than a masked failure — but it is held for only
    ``_GITHUB_AUTH_NEGATIVE_TTL_SECONDS`` so a transient gh failure cannot hide
    a working GitHub setup for the full TTL.

    *probe* overrides the callable used on a miss. It exists so an API layer
    that resolves ``auth_service`` through its own module namespace (and whose
    tests patch it there) shares this one cache instead of keeping a second.

    This is a **blocking** call on a miss (``gh auth status``, 10s timeout) —
    async callers must run it in a thread.
    """
    global _github_auth_cache

    now = time.monotonic()
    cached = _github_auth_cache
    if cached is not None:
        ttl = _GITHUB_AUTH_TTL_SECONDS if cached[0] else _GITHUB_AUTH_NEGATIVE_TTL_SECONDS
        if (now - cached[1]) < ttl:
            return cached[0]
    value = (probe or auth_service.is_github_authenticated)()
    _github_auth_cache = (value, now)
    return value


def reset_github_auth_cache() -> None:
    """Drop the cached gh signal (tests; a login/logout that wants it now)."""
    global _github_auth_cache
    _github_auth_cache = None


def set_github_auth_cached(value: bool) -> None:
    """Seed the cache with a freshly observed answer.

    Used by the login/logout/status endpoints, which have just learned the
    truth first-hand: seeding both propagates the change immediately AND avoids
    the redundant ``gh auth status`` spawn the next reader would otherwise pay.
    """
    global _github_auth_cache
    _github_auth_cache = (bool(value), time.monotonic())
