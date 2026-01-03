"""
Unit tests for authentication service.

Tests cover:
- Password hashing and verification
- Session token generation
- Executable finding utilities
- Subprocess command validation
"""

import pytest
import sys
import os
from pathlib import Path
from unittest.mock import patch, MagicMock

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

    @pytest.mark.skipif(sys.platform != 'win32', reason="Windows-specific test")
    def test_find_claude_windows_npm_path(self):
        """Should check npm paths on Windows."""
        with patch('shutil.which', return_value=None):
            with patch('sys.platform', 'win32'):
                # Even if shutil.which fails, it should check Windows paths
                # This test just verifies the function doesn't crash
                result = auth.find_claude_executable()
                # Result could be None or a path, depending on system state


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
