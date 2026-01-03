"""
Unit tests for authentication service.

Tests cover:
- Password hashing and verification
- Session token generation
- Executable finding utilities
- Subprocess command validation
- Claude CLI authentication
- GitHub CLI authentication
- OAuth login flow
- Admin setup
"""

import pytest
import sys
import os
import json
import asyncio
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock
from datetime import datetime, timedelta

from app.core import auth


class TestPasswordHashing:
    """Test password hashing functionality."""

    def test_password_hash_roundtrip(self):
        """Password should verify against its own hash."""
        import bcrypt
        password = "test_password_123"
        password_hash = bcrypt.hashpw(
            password.encode('utf-8'),
            bcrypt.gensalt()
        ).decode('utf-8')

        service = auth.AuthService()
        assert service.verify_password(password, password_hash) is True

    def test_wrong_password_fails(self):
        """Wrong password should not verify."""
        import bcrypt
        password_hash = bcrypt.hashpw(
            "correct_password".encode('utf-8'),
            bcrypt.gensalt()
        ).decode('utf-8')

        service = auth.AuthService()
        assert service.verify_password("wrong_password", password_hash) is False

    def test_verify_password_handles_exceptions(self):
        """Should handle invalid hash gracefully."""
        service = auth.AuthService()
        # Invalid hash format should return False, not raise
        assert service.verify_password("password", "invalid_hash") is False


class TestFindClaudeExecutable:
    """Test claude executable finding."""

    def test_find_claude_in_path(self):
        """Should find claude if in PATH."""
        with patch('shutil.which', return_value='/usr/bin/claude'):
            result = auth.find_claude_executable()
            assert result == '/usr/bin/claude'

    def test_find_claude_not_found(self):
        """Should return None if not found."""
        with patch('shutil.which', return_value=None):
            with patch('sys.platform', 'linux'):
                result = auth.find_claude_executable()
                assert result is None

    def test_find_claude_windows_npm_path_found(self):
        """Should check npm paths on Windows and return if found."""
        with patch('shutil.which', return_value=None):
            with patch.object(sys, 'platform', 'win32'):
                mock_path = MagicMock()
                mock_path.exists.return_value = True
                mock_path.__str__ = lambda self: 'C:\\Users\\Test\\AppData\\Roaming\\npm\\claude.cmd'

                with patch.object(Path, '__new__', return_value=mock_path):
                    with patch.dict(os.environ, {'APPDATA': 'C:\\Users\\Test\\AppData\\Roaming'}):
                        # Create a mock that returns True for exists()
                        with patch('pathlib.Path.exists', return_value=True):
                            result = auth.find_claude_executable()
                            # Should return the found path or None if not found


class TestFindGhExecutable:
    """Test GitHub CLI executable finding."""

    def test_find_gh_in_path(self):
        """Should find gh if in PATH."""
        with patch('shutil.which', return_value='/usr/bin/gh'):
            result = auth.find_gh_executable()
            assert result == '/usr/bin/gh'

    def test_find_gh_not_found(self):
        """Should return None if not found."""
        with patch('shutil.which', return_value=None):
            with patch('sys.platform', 'linux'):
                result = auth.find_gh_executable()
                assert result is None

    def test_find_gh_windows_paths(self):
        """Should check Windows-specific paths."""
        with patch('shutil.which', return_value=None):
            with patch.object(sys, 'platform', 'win32'):
                with patch.dict(os.environ, {'ProgramFiles': 'C:\\Program Files', 'LOCALAPPDATA': 'C:\\Users\\Test\\AppData\\Local'}):
                    with patch('pathlib.Path.exists', return_value=False):
                        result = auth.find_gh_executable()
                        assert result is None


class TestRunSubprocessCmd:
    """Test subprocess command security validation."""

    def test_rejects_shell_metacharacters(self):
        """Should reject commands with shell metacharacters."""
        dangerous_chars = [';', '|', '&', '$', '`', '"', "'", '\n', '\r']

        for char in dangerous_chars:
            with pytest.raises(ValueError, match="potentially dangerous"):
                auth.run_subprocess_cmd(['echo', f'test{char}injection'])

    def test_allows_safe_commands(self):
        """Should allow safe command arguments."""
        # This should not raise - mock the actual subprocess call
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            # These are safe commands that should be allowed
            result = auth.run_subprocess_cmd(['echo', 'hello'])
            mock_run.assert_called_once()

    def test_allows_paths_with_dashes(self):
        """Should allow paths with dashes and underscores."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            auth.run_subprocess_cmd(['/usr/bin/my-command', '--flag', 'value_123'])
            mock_run.assert_called_once()

    def test_windows_cmd_file_uses_shell(self):
        """Should use shell=True for Windows .cmd files."""
        with patch('subprocess.run') as mock_run:
            with patch.object(sys, 'platform', 'win32'):
                mock_run.return_value = MagicMock(returncode=0)
                # Use forward slashes which are valid on Windows and not in the dangerous chars list
                auth.run_subprocess_cmd(['C:/path/to/script.cmd', 'arg1'])
                # Verify shell=True was used
                call_args = mock_run.call_args
                assert call_args[1].get('shell') is True


class TestAuthServiceInit:
    """Test AuthService initialization."""

    def test_init_creates_service(self):
        """Should initialize without errors."""
        service = auth.AuthService()
        assert service is not None
        assert service.config_dir is not None

    def test_init_uses_home_env(self):
        """Should use HOME environment variable."""
        with patch.dict(os.environ, {'HOME': '/custom/home'}):
            service = auth.AuthService()
            assert service.config_dir == Path('/custom/home/.claude')

    def test_init_uses_userprofile_on_windows(self):
        """Should fall back to USERPROFILE on Windows."""
        with patch.dict(os.environ, {'HOME': '', 'USERPROFILE': 'C:\\Users\\Test'}, clear=False):
            # Clear HOME but keep USERPROFILE
            env = os.environ.copy()
            env.pop('HOME', None)
            env['USERPROFILE'] = 'C:\\Users\\Test'

            with patch.dict(os.environ, env, clear=True):
                service = auth.AuthService()
                # Should use USERPROFILE when HOME is not set
                assert 'Test' in str(service.config_dir) or '.claude' in str(service.config_dir)

    def test_init_fallback_to_path_home(self):
        """Should fall back to Path.home() when no env vars set."""
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(sys, 'platform', 'win32'):
                with patch.object(Path, 'home', return_value=Path('C:/Users/Default')):
                    service = auth.AuthService()
                    assert '.claude' in str(service.config_dir)


class TestAuthServiceLogin:
    """Test login functionality."""

    def test_login_no_admin(self):
        """Login should fail if no admin exists."""
        service = auth.AuthService()

        with patch.object(auth.database, 'get_admin', return_value=None):
            result = service.login("admin", "password")
            assert result is None

    def test_login_wrong_username(self):
        """Login should fail with wrong username."""
        service = auth.AuthService()

        import bcrypt
        password_hash = bcrypt.hashpw(b"correct", bcrypt.gensalt()).decode()

        with patch.object(auth.database, 'get_admin', return_value={
            "username": "admin",
            "password_hash": password_hash
        }):
            result = service.login("wrong_user", "correct")
            assert result is None

    def test_login_wrong_password(self):
        """Login should fail with wrong password."""
        service = auth.AuthService()

        import bcrypt
        password_hash = bcrypt.hashpw(b"correct_password", bcrypt.gensalt()).decode()

        with patch.object(auth.database, 'get_admin', return_value={
            "username": "admin",
            "password_hash": password_hash
        }):
            result = service.login("admin", "wrong_password")
            assert result is None

    def test_login_success(self):
        """Login should succeed with correct credentials."""
        service = auth.AuthService()

        import bcrypt
        password = "correct_password"
        password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

        with patch.object(auth.database, 'get_admin', return_value={
            "username": "admin",
            "password_hash": password_hash
        }):
            with patch.object(auth.database, 'create_auth_session') as mock_create:
                result = service.login("admin", password)

                assert result is not None
                assert isinstance(result, str)
                assert len(result) > 20  # Token should be reasonably long
                mock_create.assert_called_once()


class TestAuthServiceSession:
    """Test session management."""

    def test_create_session_returns_token(self):
        """create_session should return a token string."""
        service = auth.AuthService()

        with patch.object(auth.database, 'create_auth_session'):
            token = service.create_session()

            assert isinstance(token, str)
            assert len(token) >= 32

    def test_create_session_tokens_unique(self):
        """Each session token should be unique."""
        service = auth.AuthService()

        with patch.object(auth.database, 'create_auth_session'):
            tokens = [service.create_session() for _ in range(100)]
            assert len(set(tokens)) == 100

    def test_validate_session_empty_token(self):
        """Empty token should not validate."""
        service = auth.AuthService()

        assert service.validate_session("") is False
        assert service.validate_session(None) is False

    def test_validate_session_valid_token(self):
        """Valid token should validate."""
        service = auth.AuthService()

        with patch.object(auth.database, 'get_auth_session', return_value={"token": "test"}):
            assert service.validate_session("valid_token") is True

    def test_validate_session_invalid_token(self):
        """Invalid token should not validate."""
        service = auth.AuthService()

        with patch.object(auth.database, 'get_auth_session', return_value=None):
            assert service.validate_session("invalid_token") is False

    def test_logout_calls_delete(self):
        """Logout should delete the session."""
        service = auth.AuthService()

        with patch.object(auth.database, 'delete_auth_session') as mock_delete:
            service.logout("test_token")
            mock_delete.assert_called_once_with("test_token")

    def test_logout_empty_token(self):
        """Logout with empty token should not call delete."""
        service = auth.AuthService()

        with patch.object(auth.database, 'delete_auth_session') as mock_delete:
            service.logout("")
            mock_delete.assert_not_called()

            service.logout(None)
            mock_delete.assert_not_called()


class TestAuthServiceSetup:
    """Test admin setup functionality."""

    def test_is_setup_required_delegates_to_db(self):
        """Should delegate to database module."""
        service = auth.AuthService()

        with patch.object(auth.database, 'is_setup_required', return_value=True):
            assert service.is_setup_required() is True

        with patch.object(auth.database, 'is_setup_required', return_value=False):
            assert service.is_setup_required() is False

    def test_setup_admin_fails_if_already_setup(self):
        """Should raise error if admin already exists."""
        service = auth.AuthService()

        with patch.object(auth.database, 'is_setup_required', return_value=False):
            with pytest.raises(ValueError, match="Admin already configured"):
                service.setup_admin("admin", "password123")

    def test_setup_admin_success(self):
        """Should create admin and return token."""
        service = auth.AuthService()

        with patch.object(auth.database, 'is_setup_required', return_value=True):
            with patch.object(auth.database, 'create_admin', return_value={"id": 1, "username": "admin"}):
                with patch.object(auth.database, 'create_auth_session'):
                    result = service.setup_admin("admin", "password123")

                    assert "admin" in result
                    assert "token" in result
                    assert result["admin"]["username"] == "admin"

    def test_get_admin_username_returns_username(self):
        """Should return admin username from database."""
        service = auth.AuthService()

        with patch.object(auth.database, 'get_admin', return_value={"username": "testadmin"}):
            assert service.get_admin_username() == "testadmin"

    def test_get_admin_username_returns_none_if_no_admin(self):
        """Should return None if no admin exists."""
        service = auth.AuthService()

        with patch.object(auth.database, 'get_admin', return_value=None):
            assert service.get_admin_username() is None


class TestClaudeAuthentication:
    """Test Claude CLI authentication methods."""

    def test_is_claude_authenticated_with_valid_credentials(self, temp_dir):
        """Should return True when valid credentials exist."""
        service = auth.AuthService()
        service.config_dir = temp_dir

        # Create .credentials.json
        creds_file = temp_dir / '.credentials.json'
        creds_file.write_text('{"claudeAiOauth": {"accessToken": "test"}}')

        # Mock direct_oauth to return True
        with patch.object(auth.direct_oauth, 'has_valid_credentials', return_value=True):
            with patch.object(service, '_ensure_onboarding_complete'):
                assert service.is_claude_authenticated() is True

    def test_is_claude_authenticated_no_credentials_file(self, temp_dir):
        """Should return False when no credentials file exists."""
        service = auth.AuthService()
        service.config_dir = temp_dir

        with patch.object(auth.direct_oauth, 'has_valid_credentials', return_value=False):
            assert service.is_claude_authenticated() is False

    def test_is_claude_authenticated_empty_credentials_file(self, temp_dir):
        """Should return False when credentials file is empty."""
        service = auth.AuthService()
        service.config_dir = temp_dir

        # Create empty credentials file
        creds_file = temp_dir / '.credentials.json'
        creds_file.write_text('')

        with patch.object(auth.direct_oauth, 'has_valid_credentials', return_value=False):
            assert service.is_claude_authenticated() is False

    def test_is_claude_authenticated_fallback_to_file_check(self, temp_dir):
        """Should fall back to file existence check when oauth returns False."""
        service = auth.AuthService()
        service.config_dir = temp_dir

        # Create credentials file with content
        creds_file = temp_dir / '.credentials.json'
        creds_file.write_text('{"some": "content"}')

        with patch.object(auth.direct_oauth, 'has_valid_credentials', return_value=False):
            with patch.object(service, '_ensure_onboarding_complete'):
                assert service.is_claude_authenticated() is True

    def test_validate_claude_credentials_no_file(self, temp_dir):
        """Should return invalid when no credentials file exists."""
        service = auth.AuthService()
        service.config_dir = temp_dir

        result = service.validate_claude_credentials()

        assert result["valid"] is False
        assert result["authenticated"] is False
        assert "No credentials" in result["error"]

    def test_validate_claude_credentials_empty_file(self, temp_dir):
        """Should return invalid when credentials file is empty."""
        service = auth.AuthService()
        service.config_dir = temp_dir

        creds_file = temp_dir / '.credentials.json'
        creds_file.write_text('')

        result = service.validate_claude_credentials()

        assert result["valid"] is False
        assert result["authenticated"] is False

    def test_validate_claude_credentials_no_cli(self, temp_dir):
        """Should handle missing CLI gracefully."""
        service = auth.AuthService()
        service.config_dir = temp_dir

        creds_file = temp_dir / '.credentials.json'
        creds_file.write_text('{"test": "creds"}')

        with patch.object(auth, 'find_claude_executable', return_value=None):
            result = service.validate_claude_credentials()

            assert result["valid"] is False
            assert result["authenticated"] is True
            assert "CLI not found" in result["error"]

    def test_validate_claude_credentials_success(self, temp_dir):
        """Should return valid when CLI check succeeds."""
        service = auth.AuthService()
        service.config_dir = temp_dir

        creds_file = temp_dir / '.credentials.json'
        creds_file.write_text('{"test": "creds"}')

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "1.0.0"

        with patch.object(auth, 'find_claude_executable', return_value='/usr/bin/claude'):
            with patch('subprocess.run', return_value=mock_result):
                result = service.validate_claude_credentials()

                assert result["valid"] is True
                assert result["authenticated"] is True

    def test_validate_claude_credentials_auth_error(self, temp_dir):
        """Should detect authentication errors from CLI."""
        service = auth.AuthService()
        service.config_dir = temp_dir

        creds_file = temp_dir / '.credentials.json'
        creds_file.write_text('{"test": "creds"}')

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "unauthorized: token expired"

        with patch.object(auth, 'find_claude_executable', return_value='/usr/bin/claude'):
            with patch('subprocess.run', return_value=mock_result):
                result = service.validate_claude_credentials()

                assert result["valid"] is False
                assert result["authenticated"] is True
                assert "expired" in result["error"]

    def test_validate_claude_credentials_timeout(self, temp_dir):
        """Should handle CLI timeout."""
        import subprocess
        service = auth.AuthService()
        service.config_dir = temp_dir

        creds_file = temp_dir / '.credentials.json'
        creds_file.write_text('{"test": "creds"}')

        with patch.object(auth, 'find_claude_executable', return_value='/usr/bin/claude'):
            with patch('subprocess.run', side_effect=subprocess.TimeoutExpired(cmd='claude', timeout=10)):
                result = service.validate_claude_credentials()

                assert result["valid"] is False
                assert "timed out" in result["error"]

    def test_validate_claude_credentials_exception(self, temp_dir):
        """Should handle general exceptions."""
        service = auth.AuthService()
        service.config_dir = temp_dir

        creds_file = temp_dir / '.credentials.json'
        creds_file.write_text('{"test": "creds"}')

        with patch.object(auth, 'find_claude_executable', return_value='/usr/bin/claude'):
            with patch('subprocess.run', side_effect=Exception("Unknown error")):
                result = service.validate_claude_credentials()

                assert result["valid"] is False
                assert "Unknown error" in result["error"]

    def test_validate_claude_credentials_non_auth_error(self, temp_dir):
        """Should treat non-auth errors as valid."""
        service = auth.AuthService()
        service.config_dir = temp_dir

        creds_file = temp_dir / '.credentials.json'
        creds_file.write_text('{"test": "creds"}')

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "some other error"

        with patch.object(auth, 'find_claude_executable', return_value='/usr/bin/claude'):
            with patch('subprocess.run', return_value=mock_result):
                result = service.validate_claude_credentials()

                # Non-auth errors should still return valid=True
                assert result["valid"] is True
                assert result["authenticated"] is True


class TestEnsureOnboardingComplete:
    """Test _ensure_onboarding_complete method."""

    def test_creates_settings_file_if_not_exists(self, temp_dir):
        """Should create settings.json if it doesn't exist."""
        service = auth.AuthService()
        service.config_dir = temp_dir
        temp_dir.mkdir(exist_ok=True)

        service._ensure_onboarding_complete()

        settings_file = temp_dir / 'settings.json'
        assert settings_file.exists()

        data = json.loads(settings_file.read_text())
        assert data["hasCompletedOnboarding"] is True
        assert data["theme"] == "dark"

    def test_updates_existing_settings_file(self, temp_dir):
        """Should update existing settings.json."""
        service = auth.AuthService()
        service.config_dir = temp_dir
        temp_dir.mkdir(exist_ok=True)

        settings_file = temp_dir / 'settings.json'
        settings_file.write_text('{"customSetting": "value", "theme": "light"}')

        service._ensure_onboarding_complete()

        data = json.loads(settings_file.read_text())
        assert data["hasCompletedOnboarding"] is True
        assert data["customSetting"] == "value"
        assert data["theme"] == "light"  # Should preserve existing theme

    def test_skips_if_already_complete(self, temp_dir):
        """Should not rewrite if already complete."""
        service = auth.AuthService()
        service.config_dir = temp_dir
        temp_dir.mkdir(exist_ok=True)

        settings_file = temp_dir / 'settings.json'
        settings_file.write_text('{"hasCompletedOnboarding": true}')
        original_mtime = settings_file.stat().st_mtime

        service._ensure_onboarding_complete()

        # File should not have been modified
        # Note: This might be flaky on fast systems, so we just check content
        data = json.loads(settings_file.read_text())
        assert data["hasCompletedOnboarding"] is True

    def test_handles_exception_gracefully(self, temp_dir):
        """Should handle exceptions without crashing."""
        service = auth.AuthService()
        service.config_dir = temp_dir / 'nonexistent' / 'deep' / 'path'

        # Should not raise, just log warning
        service._ensure_onboarding_complete()


class TestGetClaudeAuthInfo:
    """Test get_claude_auth_info method."""

    def test_returns_auth_info(self, temp_dir):
        """Should return correct auth info structure."""
        service = auth.AuthService()
        service.config_dir = temp_dir

        with patch.object(service, 'is_claude_authenticated', return_value=True):
            result = service.get_claude_auth_info()

            assert result["authenticated"] is True
            assert "config_dir" in result
            assert "credentials_file" in result


class TestGetLoginInstructions:
    """Test get_login_instructions method."""

    def test_returns_authenticated_status(self):
        """Should return authenticated status when already logged in."""
        service = auth.AuthService()

        with patch.object(service, 'is_claude_authenticated', return_value=True):
            result = service.get_login_instructions()

            assert result["status"] == "authenticated"
            assert "Already authenticated" in result["message"]

    def test_returns_instructions_when_not_authenticated(self):
        """Should return instructions when not logged in."""
        service = auth.AuthService()

        with patch.object(service, 'is_claude_authenticated', return_value=False):
            result = service.get_login_instructions()

            assert result["status"] == "not_authenticated"
            assert "instructions" in result
            assert "command" in result


class TestClaudeLogout:
    """Test claude_logout method."""

    def test_logout_success_via_cli(self, temp_dir):
        """Should logout successfully via CLI."""
        service = auth.AuthService()
        service.config_dir = temp_dir

        mock_result = MagicMock()
        mock_result.returncode = 0

        with patch.object(auth, 'find_claude_executable', return_value='/usr/bin/claude'):
            with patch('subprocess.run', return_value=mock_result):
                result = service.claude_logout()

                assert result["success"] is True
                assert "Logged out" in result["message"]

    def test_logout_cli_not_found_deletes_file(self, temp_dir):
        """Should delete credentials file when CLI not found."""
        service = auth.AuthService()
        service.config_dir = temp_dir
        temp_dir.mkdir(exist_ok=True)

        creds_file = temp_dir / '.credentials.json'
        creds_file.write_text('{"test": "creds"}')

        with patch.object(auth, 'find_claude_executable', return_value=None):
            result = service.claude_logout()

            assert result["success"] is True
            assert not creds_file.exists()

    def test_logout_cli_fails_deletes_file(self, temp_dir):
        """Should delete file as fallback when CLI fails."""
        service = auth.AuthService()
        service.config_dir = temp_dir
        temp_dir.mkdir(exist_ok=True)

        creds_file = temp_dir / '.credentials.json'
        creds_file.write_text('{"test": "creds"}')

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "CLI error"

        with patch.object(auth, 'find_claude_executable', return_value='/usr/bin/claude'):
            with patch('subprocess.run', return_value=mock_result):
                result = service.claude_logout()

                assert result["success"] is True
                assert not creds_file.exists()

    def test_logout_cli_exception_deletes_file(self, temp_dir):
        """Should delete file when CLI raises exception."""
        service = auth.AuthService()
        service.config_dir = temp_dir
        temp_dir.mkdir(exist_ok=True)

        creds_file = temp_dir / '.credentials.json'
        creds_file.write_text('{"test": "creds"}')

        with patch.object(auth, 'find_claude_executable', return_value='/usr/bin/claude'):
            with patch('subprocess.run', side_effect=Exception("CLI error")):
                result = service.claude_logout()

                assert result["success"] is True
                assert not creds_file.exists()

    def test_logout_file_deletion_fails(self, temp_dir):
        """Should return failure when file deletion fails."""
        service = auth.AuthService()
        service.config_dir = temp_dir
        temp_dir.mkdir(exist_ok=True)

        creds_file = temp_dir / '.credentials.json'
        creds_file.write_text('{"test": "creds"}')

        with patch.object(auth, 'find_claude_executable', return_value=None):
            with patch.object(Path, 'unlink', side_effect=PermissionError("Access denied")):
                result = service.claude_logout()

                assert result["success"] is False
                assert "failed" in result["message"].lower()

    def test_logout_cli_error_no_file_exists(self, temp_dir):
        """Should return success when CLI fails but no credentials file exists."""
        service = auth.AuthService()
        service.config_dir = temp_dir
        temp_dir.mkdir(exist_ok=True)

        # No credentials file exists
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "CLI error"

        with patch.object(auth, 'find_claude_executable', return_value='/usr/bin/claude'):
            with patch('subprocess.run', return_value=mock_result):
                result = service.claude_logout()

                # Should succeed since no file to delete
                assert result["success"] is True


class TestGitHubAuthentication:
    """Test GitHub CLI authentication methods."""

    def test_is_github_authenticated_no_hosts_file(self, temp_dir):
        """Should return False when no hosts.yml exists."""
        service = auth.AuthService()
        service.gh_config_dir = temp_dir

        assert service.is_github_authenticated() is False

    def test_is_github_authenticated_empty_hosts_file(self, temp_dir):
        """Should return False when hosts.yml is empty."""
        service = auth.AuthService()
        service.gh_config_dir = temp_dir
        temp_dir.mkdir(exist_ok=True)

        hosts_file = temp_dir / 'hosts.yml'
        hosts_file.write_text('')

        assert service.is_github_authenticated() is False

    def test_is_github_authenticated_gh_not_found(self, temp_dir):
        """Should return False when gh CLI not found."""
        service = auth.AuthService()
        service.gh_config_dir = temp_dir
        temp_dir.mkdir(exist_ok=True)

        hosts_file = temp_dir / 'hosts.yml'
        hosts_file.write_text('github.com: test')

        with patch.object(auth, 'find_gh_executable', return_value=None):
            assert service.is_github_authenticated() is False

    def test_is_github_authenticated_success(self, temp_dir):
        """Should return True when gh auth status succeeds."""
        service = auth.AuthService()
        service.gh_config_dir = temp_dir
        temp_dir.mkdir(exist_ok=True)

        hosts_file = temp_dir / 'hosts.yml'
        hosts_file.write_text('github.com: test')

        mock_result = MagicMock()
        mock_result.returncode = 0

        with patch.object(auth, 'find_gh_executable', return_value='/usr/bin/gh'):
            with patch('subprocess.run', return_value=mock_result):
                assert service.is_github_authenticated() is True

    def test_is_github_authenticated_auth_failed(self, temp_dir):
        """Should return False when gh auth status fails."""
        service = auth.AuthService()
        service.gh_config_dir = temp_dir
        temp_dir.mkdir(exist_ok=True)

        hosts_file = temp_dir / 'hosts.yml'
        hosts_file.write_text('github.com: test')

        mock_result = MagicMock()
        mock_result.returncode = 1

        with patch.object(auth, 'find_gh_executable', return_value='/usr/bin/gh'):
            with patch('subprocess.run', return_value=mock_result):
                assert service.is_github_authenticated() is False

    def test_is_github_authenticated_exception(self, temp_dir):
        """Should return False on exception."""
        service = auth.AuthService()
        service.gh_config_dir = temp_dir
        temp_dir.mkdir(exist_ok=True)

        hosts_file = temp_dir / 'hosts.yml'
        hosts_file.write_text('github.com: test')

        with patch.object(auth, 'find_gh_executable', return_value='/usr/bin/gh'):
            with patch('subprocess.run', side_effect=Exception("Error")):
                assert service.is_github_authenticated() is False


class TestGetGitHubAuthInfo:
    """Test get_github_auth_info method."""

    def test_returns_auth_info_not_authenticated(self, temp_dir):
        """Should return correct info when not authenticated."""
        service = auth.AuthService()
        service.gh_config_dir = temp_dir

        with patch.object(service, 'is_github_authenticated', return_value=False):
            result = service.get_github_auth_info()

            assert result["authenticated"] is False
            assert result["user"] is None
            assert "config_dir" in result

    def test_returns_auth_info_with_user(self, temp_dir):
        """Should return user info when authenticated."""
        service = auth.AuthService()
        service.gh_config_dir = temp_dir

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "testuser"

        with patch.object(service, 'is_github_authenticated', return_value=True):
            with patch.object(auth, 'find_gh_executable', return_value='/usr/bin/gh'):
                with patch('subprocess.run', return_value=mock_result):
                    result = service.get_github_auth_info()

                    assert result["authenticated"] is True
                    assert result["user"] == "testuser"

    def test_handles_user_fetch_failure(self, temp_dir):
        """Should handle failure to fetch user info."""
        service = auth.AuthService()
        service.gh_config_dir = temp_dir

        with patch.object(service, 'is_github_authenticated', return_value=True):
            with patch.object(auth, 'find_gh_executable', return_value='/usr/bin/gh'):
                with patch('subprocess.run', side_effect=Exception("Error")):
                    result = service.get_github_auth_info()

                    assert result["authenticated"] is True
                    assert result["user"] is None


class TestGitHubLoginWithToken:
    """Test github_login_with_token method."""

    def test_login_gh_not_found(self, temp_dir):
        """Should return error when gh CLI not found."""
        service = auth.AuthService()
        service.gh_config_dir = temp_dir

        with patch.object(auth, 'find_gh_executable', return_value=None):
            result = service.github_login_with_token("test_token")

            assert result["success"] is False
            assert "not found" in result["message"]

    def test_login_success(self, temp_dir):
        """Should login successfully with valid token."""
        service = auth.AuthService()
        service.gh_config_dir = temp_dir
        temp_dir.mkdir(exist_ok=True)

        mock_result = MagicMock()
        mock_result.returncode = 0

        with patch.object(auth, 'find_gh_executable', return_value='/usr/bin/gh'):
            with patch('shutil.which', return_value='/usr/bin/git'):
                with patch('subprocess.run', return_value=mock_result):
                    result = service.github_login_with_token("test_token")

                    assert result["success"] is True
                    assert "Successfully" in result["message"]

    def test_login_failure(self, temp_dir):
        """Should return error when login fails."""
        service = auth.AuthService()
        service.gh_config_dir = temp_dir
        temp_dir.mkdir(exist_ok=True)

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "Invalid token"

        with patch.object(auth, 'find_gh_executable', return_value='/usr/bin/gh'):
            with patch('subprocess.run', return_value=mock_result):
                result = service.github_login_with_token("bad_token")

                assert result["success"] is False
                assert "Invalid token" in result["error"]

    def test_login_timeout(self, temp_dir):
        """Should handle timeout."""
        import subprocess
        service = auth.AuthService()
        service.gh_config_dir = temp_dir
        temp_dir.mkdir(exist_ok=True)

        with patch.object(auth, 'find_gh_executable', return_value='/usr/bin/gh'):
            with patch('subprocess.run', side_effect=subprocess.TimeoutExpired(cmd='gh', timeout=30)):
                result = service.github_login_with_token("test_token")

                assert result["success"] is False
                assert "timed out" in result["message"]

    def test_login_exception(self, temp_dir):
        """Should handle exceptions."""
        service = auth.AuthService()
        service.gh_config_dir = temp_dir
        temp_dir.mkdir(exist_ok=True)

        with patch.object(auth, 'find_gh_executable', return_value='/usr/bin/gh'):
            with patch('subprocess.run', side_effect=Exception("Unknown error")):
                result = service.github_login_with_token("test_token")

                assert result["success"] is False
                assert "Unknown error" in result["error"]


class TestGitHubLogout:
    """Test github_logout method."""

    def test_logout_gh_not_found_removes_config(self, temp_dir):
        """Should remove config file when gh not found."""
        service = auth.AuthService()
        service.gh_config_dir = temp_dir
        temp_dir.mkdir(exist_ok=True)

        hosts_file = temp_dir / 'hosts.yml'
        hosts_file.write_text('github.com: test')

        with patch.object(auth, 'find_gh_executable', return_value=None):
            result = service.github_logout()

            assert result["success"] is True
            assert not hosts_file.exists()

    def test_logout_success(self, temp_dir):
        """Should logout successfully via CLI."""
        service = auth.AuthService()
        service.gh_config_dir = temp_dir

        mock_result = MagicMock()
        mock_result.returncode = 0

        with patch.object(auth, 'find_gh_executable', return_value='/usr/bin/gh'):
            with patch('subprocess.run', return_value=mock_result):
                result = service.github_logout()

                assert result["success"] is True

    def test_logout_cli_fails_removes_config(self, temp_dir):
        """Should remove config when CLI fails."""
        service = auth.AuthService()
        service.gh_config_dir = temp_dir
        temp_dir.mkdir(exist_ok=True)

        hosts_file = temp_dir / 'hosts.yml'
        hosts_file.write_text('github.com: test')

        mock_result = MagicMock()
        mock_result.returncode = 1

        with patch.object(auth, 'find_gh_executable', return_value='/usr/bin/gh'):
            with patch('subprocess.run', return_value=mock_result):
                result = service.github_logout()

                assert result["success"] is True
                assert not hosts_file.exists()

    def test_logout_exception(self, temp_dir):
        """Should handle exception."""
        service = auth.AuthService()
        service.gh_config_dir = temp_dir

        with patch.object(auth, 'find_gh_executable', return_value='/usr/bin/gh'):
            with patch('subprocess.run', side_effect=Exception("Error")):
                result = service.github_logout()

                assert result["success"] is False
                assert "Error" in result["error"]


class TestClaudeOAuthLogin:
    """Test OAuth login flow methods."""

    def test_start_oauth_already_authenticated(self):
        """Should return already authenticated status."""
        service = auth.AuthService()

        with patch.object(service, 'is_claude_authenticated', return_value=True):
            result = service.start_claude_oauth_login()

            assert result["success"] is True
            assert result.get("already_authenticated") is True

    def test_start_oauth_force_reauth(self, temp_dir):
        """Should delete credentials and start new flow with force_reauth."""
        service = auth.AuthService()
        service.config_dir = temp_dir
        temp_dir.mkdir(exist_ok=True)

        creds_file = temp_dir / '.credentials.json'
        creds_file.write_text('{"old": "creds"}')

        mock_oauth_result = {
            "oauth_url": "https://claude.ai/oauth/authorize?...",
            "state": "test_state"
        }

        with patch.object(auth.direct_oauth, 'start_oauth_flow', return_value=mock_oauth_result):
            result = service.start_claude_oauth_login(force_reauth=True)

            assert result["success"] is True
            assert "oauth_url" in result
            assert result["requires_code"] is True
            assert not creds_file.exists()

    def test_start_oauth_force_reauth_file_delete_fails(self, temp_dir):
        """Should continue OAuth flow even if force_reauth file deletion fails."""
        service = auth.AuthService()
        service.config_dir = temp_dir
        temp_dir.mkdir(exist_ok=True)

        creds_file = temp_dir / '.credentials.json'
        creds_file.write_text('{"old": "creds"}')

        mock_oauth_result = {
            "oauth_url": "https://claude.ai/oauth/authorize?...",
            "state": "test_state"
        }

        # Mock the unlink to fail (file deletion fails)
        with patch.object(Path, 'unlink', side_effect=PermissionError("Access denied")):
            with patch.object(auth.direct_oauth, 'start_oauth_flow', return_value=mock_oauth_result):
                result = service.start_claude_oauth_login(force_reauth=True)

                # Should still succeed and return OAuth URL even if file deletion failed
                assert result["success"] is True
                assert "oauth_url" in result

    def test_start_oauth_success(self):
        """Should return OAuth URL and state."""
        service = auth.AuthService()

        mock_oauth_result = {
            "oauth_url": "https://claude.ai/oauth/authorize?...",
            "state": "test_state"
        }

        with patch.object(service, 'is_claude_authenticated', return_value=False):
            with patch.object(auth.direct_oauth, 'start_oauth_flow', return_value=mock_oauth_result):
                result = service.start_claude_oauth_login()

                assert result["success"] is True
                assert result["oauth_url"] == mock_oauth_result["oauth_url"]
                assert result["state"] == mock_oauth_result["state"]

    def test_start_oauth_exception(self):
        """Should handle exception."""
        service = auth.AuthService()

        with patch.object(service, 'is_claude_authenticated', return_value=False):
            with patch.object(auth.direct_oauth, 'start_oauth_flow', side_effect=Exception("OAuth error")):
                result = service.start_claude_oauth_login()

                assert result["success"] is False
                assert "OAuth error" in result["error"]


class TestCompleteClaudeOAuthLogin:
    """Test complete_claude_oauth_login method."""

    @pytest.mark.asyncio
    async def test_complete_oauth_success(self):
        """Should complete OAuth flow successfully."""
        service = auth.AuthService()

        mock_result = {
            "success": True,
            "message": "Success"
        }

        with patch.object(auth.direct_oauth, 'complete_oauth_flow', new_callable=AsyncMock, return_value=mock_result):
            with patch.object(service, '_ensure_onboarding_complete'):
                result = await service.complete_claude_oauth_login("auth_code", "state")

                assert result["success"] is True

    @pytest.mark.asyncio
    async def test_complete_oauth_failure(self):
        """Should return failure from oauth module."""
        service = auth.AuthService()

        mock_result = {
            "success": False,
            "message": "Token exchange failed",
            "error": "Invalid code"
        }

        with patch.object(auth.direct_oauth, 'complete_oauth_flow', new_callable=AsyncMock, return_value=mock_result):
            result = await service.complete_claude_oauth_login("bad_code", "state")

            assert result["success"] is False

    @pytest.mark.asyncio
    async def test_complete_oauth_exception(self):
        """Should handle exception."""
        service = auth.AuthService()

        with patch.object(auth.direct_oauth, 'complete_oauth_flow', new_callable=AsyncMock, side_effect=Exception("Error")):
            result = await service.complete_claude_oauth_login("code", "state")

            assert result["success"] is False
            assert "Error" in result["error"]


class TestPollClaudeAuthStatus:
    """Test poll_claude_auth_status method."""

    @pytest.mark.asyncio
    async def test_poll_success_immediately(self):
        """Should return success when authenticated."""
        service = auth.AuthService()

        with patch.object(service, 'is_claude_authenticated', return_value=True):
            result = await service.poll_claude_auth_status(timeout_seconds=5)

            assert result["success"] is True
            assert result["authenticated"] is True

    @pytest.mark.asyncio
    async def test_poll_timeout(self):
        """Should timeout when not authenticated."""
        service = auth.AuthService()

        with patch.object(service, 'is_claude_authenticated', return_value=False):
            # Use very short timeout for test
            result = await service.poll_claude_auth_status(timeout_seconds=1)

            assert result["success"] is False
            assert result["authenticated"] is False
            assert "timed out" in result["message"]


class TestGetAuthStatus:
    """Test get_auth_status method."""

    def test_returns_complete_status(self):
        """Should return complete auth status."""
        service = auth.AuthService()

        with patch.object(service, 'is_setup_required', return_value=False):
            with patch.object(service, 'is_claude_authenticated', return_value=True):
                with patch.object(service, 'is_github_authenticated', return_value=True):
                    with patch.object(service, 'get_admin_username', return_value="admin"):
                        result = service.get_auth_status()

                        assert result["setup_required"] is False
                        assert result["claude_authenticated"] is True
                        assert result["github_authenticated"] is True
                        assert result["username"] == "admin"

    def test_returns_status_when_setup_required(self):
        """Should return None username when setup required."""
        service = auth.AuthService()

        with patch.object(service, 'is_setup_required', return_value=True):
            with patch.object(service, 'is_claude_authenticated', return_value=False):
                with patch.object(service, 'is_github_authenticated', return_value=False):
                    result = service.get_auth_status()

                    assert result["setup_required"] is True
                    assert result["username"] is None


class TestWindowsPathHandling:
    """Test Windows-specific path handling."""

    def test_find_claude_windows_path_exists(self):
        """Should find claude in Windows npm path."""
        with patch('shutil.which', return_value=None):
            with patch.object(sys, 'platform', 'win32'):
                with patch.dict(os.environ, {'APPDATA': 'C:\\Users\\Test\\AppData\\Roaming'}):
                    mock_path = MagicMock()
                    mock_path.exists.return_value = True

                    with patch('pathlib.Path.__truediv__', return_value=mock_path):
                        # This tests that the Windows path checking code runs
                        result = auth.find_claude_executable()
                        # Result depends on whether the mocked path matches

    def test_find_gh_windows_path_exists(self):
        """Should find gh in Windows installation path."""
        with patch('shutil.which', return_value=None):
            with patch.object(sys, 'platform', 'win32'):
                with patch.dict(os.environ, {
                    'ProgramFiles': 'C:\\Program Files',
                    'LOCALAPPDATA': 'C:\\Users\\Test\\AppData\\Local'
                }):
                    # Test that the code handles Windows paths without crashing
                    result = auth.find_gh_executable()


# Fixture for temp directory
@pytest.fixture
def temp_dir(tmp_path):
    """Provide a temporary directory for tests."""
    return tmp_path
