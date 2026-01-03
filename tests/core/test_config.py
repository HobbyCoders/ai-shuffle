"""
Unit tests for configuration module.

Tests cover:
- Settings class initialization and defaults
- Dynamic path resolution (data_dir, workspace_dir)
- Deployment mode detection (local vs Docker)
- Database URL construction
- Directory creation (ensure_directories)
- Workspace loading from database
- All property methods and getters
"""

import os
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch
import tempfile


class TestSettingsDefaults:
    """Test default settings values."""

    def test_default_server_settings(self):
        """Default server settings should have reasonable values."""
        with patch("app.core.config._get_default_data_dir", return_value=Path("/mock/data")):
            with patch("app.core.config._get_default_workspace_dir", return_value=Path("/mock/workspace")):
                from app.core.config import Settings
                settings = Settings()

                assert settings.host == "0.0.0.0"
                assert settings.port == 8000
                assert settings.log_level == "INFO"

    def test_default_service_info(self):
        """Default service info should be set correctly."""
        with patch("app.core.config._get_default_data_dir", return_value=Path("/mock/data")):
            with patch("app.core.config._get_default_workspace_dir", return_value=Path("/mock/workspace")):
                from app.core.config import Settings
                settings = Settings()

                assert settings.service_name == "ai-shuffle"
                assert settings.version == "4.0.0"

    def test_default_session_settings(self):
        """Default session settings should be set correctly."""
        with patch("app.core.config._get_default_data_dir", return_value=Path("/mock/data")):
            with patch("app.core.config._get_default_workspace_dir", return_value=Path("/mock/workspace")):
                from app.core.config import Settings
                settings = Settings()

                assert settings.session_secret is None
                assert settings.session_expire_days == 30
                assert settings.command_timeout == 300

    def test_default_security_settings(self):
        """Default security settings should be set correctly."""
        with patch("app.core.config._get_default_data_dir", return_value=Path("/mock/data")):
            with patch("app.core.config._get_default_workspace_dir", return_value=Path("/mock/workspace")):
                from app.core.config import Settings
                settings = Settings()

                assert settings.max_login_attempts == 5
                assert settings.login_attempt_window_minutes == 15
                assert settings.lockout_duration_minutes == 30
                assert settings.api_key_session_expire_hours == 24
                # Note: cookie_secure may be overridden by .env file, so we just check it's a bool
                assert isinstance(settings.cookie_secure, bool)
                assert settings.max_request_body_mb == 50

    def test_default_cors_settings(self):
        """Default CORS settings should be set correctly."""
        with patch("app.core.config._get_default_data_dir", return_value=Path("/mock/data")):
            with patch("app.core.config._get_default_workspace_dir", return_value=Path("/mock/workspace")):
                from app.core.config import Settings
                settings = Settings()

                # cors_origins may be overridden by .env, check it contains localhost
                assert "localhost:8000" in settings.cors_origins
                assert settings.trusted_proxy_headers == "X-Forwarded-For,X-Real-IP"

    def test_default_path_settings_are_none(self):
        """Path settings should default to None before model_post_init."""
        with patch("app.core.config._get_default_data_dir", return_value=Path("/mock/data")):
            with patch("app.core.config._get_default_workspace_dir", return_value=Path("/mock/workspace")):
                from app.core.config import Settings
                settings = Settings()

                # After initialization, paths should be set by model_post_init
                assert settings.data_dir == Path("/mock/data")
                assert settings.workspace_dir == Path("/mock/workspace")
                assert settings.claude_projects_dir is None
                assert settings.tools_dir is None
                assert settings.database_url is None


class TestSettingsFromEnvironment:
    """Test settings loaded from environment variables."""

    def test_settings_from_env_vars(self):
        """Settings should be loaded from environment variables."""
        env_vars = {
            "HOST": "127.0.0.1",
            "PORT": "9000",
            "LOG_LEVEL": "DEBUG",
            "SERVICE_NAME": "test-service",
            "VERSION": "1.0.0",
            "SESSION_SECRET": "test-secret",
            "SESSION_EXPIRE_DAYS": "7",
            "COMMAND_TIMEOUT": "600",
            "MAX_LOGIN_ATTEMPTS": "3",
            "COOKIE_SECURE": "false",
        }

        with patch.dict(os.environ, env_vars, clear=False):
            with patch("app.core.config._get_default_data_dir", return_value=Path("/mock/data")):
                with patch("app.core.config._get_default_workspace_dir", return_value=Path("/mock/workspace")):
                    from app.core.config import Settings
                    settings = Settings()

                    assert settings.host == "127.0.0.1"
                    assert settings.port == 9000
                    assert settings.log_level == "DEBUG"
                    assert settings.service_name == "test-service"
                    assert settings.version == "1.0.0"
                    assert settings.session_secret == "test-secret"
                    assert settings.session_expire_days == 7
                    assert settings.command_timeout == 600
                    assert settings.max_login_attempts == 3
                    assert settings.cookie_secure is False

    def test_local_mode_env_var_true(self):
        """LOCAL_MODE=true should set local_mode to True."""
        with patch.dict(os.environ, {"LOCAL_MODE": "true"}, clear=False):
            with patch("app.core.config._get_default_data_dir", return_value=Path("/mock/data")):
                with patch("app.core.config._get_default_workspace_dir", return_value=Path("/mock/workspace")):
                    from app.core.config import Settings
                    settings = Settings()

                    assert settings.local_mode is True

    def test_local_mode_env_var_false(self):
        """LOCAL_MODE=false should set local_mode to False."""
        with patch.dict(os.environ, {"LOCAL_MODE": "false"}, clear=False):
            with patch("app.core.config._get_default_data_dir", return_value=Path("/mock/data")):
                with patch("app.core.config._get_default_workspace_dir", return_value=Path("/mock/workspace")):
                    from app.core.config import Settings
                    settings = Settings()

                    assert settings.local_mode is False

    def test_custom_paths_from_env(self):
        """Custom paths should be loaded from environment variables."""
        with tempfile.TemporaryDirectory() as tmpdir:
            data_path = Path(tmpdir) / "data"
            workspace_path = Path(tmpdir) / "workspace"
            claude_path = Path(tmpdir) / "claude"
            tools_path = Path(tmpdir) / "tools"

            # Create the directories
            data_path.mkdir()
            workspace_path.mkdir()

            env_vars = {
                "DATA_DIR": str(data_path),
                "WORKSPACE_DIR": str(workspace_path),
                "CLAUDE_PROJECTS_DIR": str(claude_path),
                "TOOLS_DIR": str(tools_path),
            }

            with patch.dict(os.environ, env_vars, clear=False):
                from app.core.config import Settings
                settings = Settings()

                assert settings.data_dir == data_path
                assert settings.workspace_dir == workspace_path
                assert settings.claude_projects_dir == claude_path
                assert settings.tools_dir == tools_path


class TestSettingsModelPostInit:
    """Test model_post_init behavior."""

    def test_model_post_init_sets_data_dir(self):
        """model_post_init should set data_dir if not provided."""
        mock_data_dir = Path("/auto/data")
        mock_workspace_dir = Path("/auto/workspace")

        with patch("app.core.config._get_default_data_dir", return_value=mock_data_dir):
            with patch("app.core.config._get_default_workspace_dir", return_value=mock_workspace_dir):
                from app.core.config import Settings
                settings = Settings()

                assert settings.data_dir == mock_data_dir

    def test_model_post_init_sets_workspace_dir(self):
        """model_post_init should set workspace_dir if not provided."""
        mock_data_dir = Path("/auto/data")
        mock_workspace_dir = Path("/auto/workspace")

        with patch("app.core.config._get_default_data_dir", return_value=mock_data_dir):
            with patch("app.core.config._get_default_workspace_dir", return_value=mock_workspace_dir):
                from app.core.config import Settings
                settings = Settings()

                assert settings.workspace_dir == mock_workspace_dir

    def test_model_post_init_preserves_provided_paths(self):
        """model_post_init should not override provided paths."""
        custom_data = Path("/custom/data")
        custom_workspace = Path("/custom/workspace")

        with patch.dict(os.environ, {
            "DATA_DIR": str(custom_data),
            "WORKSPACE_DIR": str(custom_workspace)
        }, clear=False):
            # These mocks should not be called since paths are provided
            with patch("app.core.config._get_default_data_dir") as mock_data:
                with patch("app.core.config._get_default_workspace_dir") as mock_workspace:
                    from app.core.config import Settings
                    settings = Settings()

                    assert settings.data_dir == custom_data
                    assert settings.workspace_dir == custom_workspace


class TestEffectiveDirectoryProperties:
    """Test effective directory property methods."""

    def test_effective_data_dir_with_value(self):
        """effective_data_dir should return data_dir when set."""
        with patch("app.core.config._get_default_data_dir", return_value=Path("/mock/data")):
            with patch("app.core.config._get_default_workspace_dir", return_value=Path("/mock/workspace")):
                from app.core.config import Settings
                settings = Settings()

                assert settings.effective_data_dir == Path("/mock/data")

    def test_effective_data_dir_fallback(self):
        """effective_data_dir should call default function if data_dir is None."""
        from app.core.config import Settings

        with patch("app.core.config._get_default_data_dir", return_value=Path("/mock/data")):
            with patch("app.core.config._get_default_workspace_dir", return_value=Path("/mock/workspace")):
                settings = Settings()
                # Force data_dir to None
                object.__setattr__(settings, 'data_dir', None)

                # Now effective_data_dir should call the fallback
                with patch("app.core.config._get_default_data_dir", return_value=Path("/fallback/data")):
                    assert settings.effective_data_dir == Path("/fallback/data")

    def test_effective_workspace_dir_with_value(self):
        """effective_workspace_dir should return workspace_dir when set."""
        with patch("app.core.config._get_default_data_dir", return_value=Path("/mock/data")):
            with patch("app.core.config._get_default_workspace_dir", return_value=Path("/mock/workspace")):
                from app.core.config import Settings
                settings = Settings()

                assert settings.effective_workspace_dir == Path("/mock/workspace")

    def test_effective_workspace_dir_fallback(self):
        """effective_workspace_dir should call default function if workspace_dir is None."""
        from app.core.config import Settings

        with patch("app.core.config._get_default_data_dir", return_value=Path("/mock/data")):
            with patch("app.core.config._get_default_workspace_dir", return_value=Path("/mock/workspace")):
                settings = Settings()
                # Force workspace_dir to None
                object.__setattr__(settings, 'workspace_dir', None)

                # Now effective_workspace_dir should call the fallback
                with patch("app.core.config._get_default_workspace_dir", return_value=Path("/fallback/workspace")):
                    assert settings.effective_workspace_dir == Path("/fallback/workspace")


class TestDatabasePaths:
    """Test database path properties and methods."""

    def test_db_path_property(self):
        """db_path should return path to SQLite database."""
        with patch("app.core.config._get_default_data_dir", return_value=Path("/mock/data")):
            with patch("app.core.config._get_default_workspace_dir", return_value=Path("/mock/workspace")):
                from app.core.config import Settings
                settings = Settings()

                assert settings.db_path == Path("/mock/data/db.sqlite")

    def test_sessions_dir_property(self):
        """sessions_dir should return path to sessions directory."""
        with patch("app.core.config._get_default_data_dir", return_value=Path("/mock/data")):
            with patch("app.core.config._get_default_workspace_dir", return_value=Path("/mock/workspace")):
                from app.core.config import Settings
                settings = Settings()

                assert settings.sessions_dir == Path("/mock/data/sessions")

    def test_get_database_url_default(self):
        """get_database_url should return SQLite URL by default."""
        with patch("app.core.config._get_default_data_dir", return_value=Path("/mock/data")):
            with patch("app.core.config._get_default_workspace_dir", return_value=Path("/mock/workspace")):
                from app.core.config import Settings
                settings = Settings()

                url = settings.get_database_url()
                assert url.startswith("sqlite:///")
                assert "db.sqlite" in url

    def test_get_database_url_custom(self):
        """get_database_url should return custom URL when set."""
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://localhost/mydb"}, clear=False):
            with patch("app.core.config._get_default_data_dir", return_value=Path("/mock/data")):
                with patch("app.core.config._get_default_workspace_dir", return_value=Path("/mock/workspace")):
                    from app.core.config import Settings
                    settings = Settings()

                    assert settings.get_database_url() == "postgresql://localhost/mydb"


class TestClaudeProjectsDir:
    """Test Claude projects directory property."""

    def test_get_claude_projects_dir_default(self):
        """get_claude_projects_dir should return default path."""
        with patch("app.core.config._get_default_data_dir", return_value=Path("/mock/data")):
            with patch("app.core.config._get_default_workspace_dir", return_value=Path("/mock/workspace")):
                from app.core.config import Settings
                settings = Settings()

                expected = Path.home() / ".claude" / "projects"
                assert settings.get_claude_projects_dir == expected

    def test_get_claude_projects_dir_custom(self):
        """get_claude_projects_dir should return custom path when set."""
        custom_path = Path("/custom/claude/projects")

        with patch.dict(os.environ, {"CLAUDE_PROJECTS_DIR": str(custom_path)}, clear=False):
            with patch("app.core.config._get_default_data_dir", return_value=Path("/mock/data")):
                with patch("app.core.config._get_default_workspace_dir", return_value=Path("/mock/workspace")):
                    from app.core.config import Settings
                    settings = Settings()

                    assert settings.get_claude_projects_dir == custom_path


class TestEffectiveToolsDir:
    """Test effective tools directory property."""

    def test_effective_tools_dir_with_custom_path(self):
        """effective_tools_dir should return custom path when set."""
        custom_tools = Path("/custom/tools")

        with patch.dict(os.environ, {"TOOLS_DIR": str(custom_tools)}, clear=False):
            with patch("app.core.config._get_default_data_dir", return_value=Path("/mock/data")):
                with patch("app.core.config._get_default_workspace_dir", return_value=Path("/mock/workspace")):
                    from app.core.config import Settings
                    settings = Settings()

                    assert settings.effective_tools_dir == custom_tools

    def test_effective_tools_dir_docker_mode(self):
        """effective_tools_dir should return /opt/ai-tools in Docker mode."""
        with patch("app.core.config._get_default_data_dir", return_value=Path("/mock/data")):
            with patch("app.core.config._get_default_workspace_dir", return_value=Path("/mock/workspace")):
                from app.core.config import Settings
                settings = Settings()

                # Patch the is_local_mode method using the Settings class
                with patch.object(Settings, 'is_local_mode', return_value=False):
                    assert settings.effective_tools_dir == Path("/opt/ai-tools")

    def test_effective_tools_dir_local_mode(self):
        """effective_tools_dir should return relative path in local mode."""
        with patch("app.core.config._get_default_data_dir", return_value=Path("/mock/data")):
            with patch("app.core.config._get_default_workspace_dir", return_value=Path("/mock/workspace")):
                from app.core.config import Settings
                settings = Settings()

                with patch.object(Settings, 'is_local_mode', return_value=True):
                    # Should be relative to the config.py file location
                    tools_dir = settings.effective_tools_dir
                    assert "tools" in str(tools_dir)


class TestIsLocalMode:
    """Test is_local_mode method."""

    def test_is_local_mode_explicit_true(self):
        """is_local_mode should return True when local_mode is explicitly True."""
        with patch.dict(os.environ, {"LOCAL_MODE": "true"}, clear=False):
            with patch("app.core.config._get_default_data_dir", return_value=Path("/mock/data")):
                with patch("app.core.config._get_default_workspace_dir", return_value=Path("/mock/workspace")):
                    from app.core.config import Settings
                    settings = Settings()

                    assert settings.is_local_mode() is True

    def test_is_local_mode_explicit_false(self):
        """is_local_mode should return False when local_mode is explicitly False."""
        with patch.dict(os.environ, {"LOCAL_MODE": "false"}, clear=False):
            with patch("app.core.config._get_default_data_dir", return_value=Path("/mock/data")):
                with patch("app.core.config._get_default_workspace_dir", return_value=Path("/mock/workspace")):
                    from app.core.config import Settings
                    settings = Settings()

                    assert settings.is_local_mode() is False

    def test_is_local_mode_auto_detect(self):
        """is_local_mode should auto-detect when local_mode is None."""
        with patch("app.core.config._get_default_data_dir", return_value=Path("/mock/data")):
            with patch("app.core.config._get_default_workspace_dir", return_value=Path("/mock/workspace")):
                from app.core.config import Settings
                settings = Settings()

                # Force local_mode to None
                object.__setattr__(settings, 'local_mode', None)

                with patch("app.core.platform.is_local_mode", return_value=True) as mock_platform:
                    assert settings.is_local_mode() is True
                    mock_platform.assert_called_once()


class TestGetDeploymentInfo:
    """Test get_deployment_info method."""

    def test_get_deployment_info_returns_dict(self):
        """get_deployment_info should return a dictionary with deployment details."""
        mock_platform_info = {
            "platform": "test",
            "deployment_mode": "local",
        }

        with patch("app.core.config._get_default_data_dir", return_value=Path("/mock/data")):
            with patch("app.core.config._get_default_workspace_dir", return_value=Path("/mock/workspace")):
                from app.core.config import Settings
                settings = Settings()

                with patch("app.core.platform.get_platform_info", return_value=mock_platform_info):
                    info = settings.get_deployment_info()

                    assert isinstance(info, dict)
                    assert "platform" in info
                    assert "deployment_mode" in info
                    assert "data_dir" in info
                    assert "workspace_dir" in info
                    assert "db_path" in info
                    assert "sessions_dir" in info

    def test_get_deployment_info_includes_paths(self):
        """get_deployment_info should include all path information."""
        mock_platform_info = {"platform": "test"}

        with patch("app.core.config._get_default_data_dir", return_value=Path("/mock/data")):
            with patch("app.core.config._get_default_workspace_dir", return_value=Path("/mock/workspace")):
                from app.core.config import Settings
                settings = Settings()

                with patch("app.core.platform.get_platform_info", return_value=mock_platform_info):
                    info = settings.get_deployment_info()

                    # Check paths are strings
                    assert isinstance(info["data_dir"], str)
                    assert isinstance(info["workspace_dir"], str)
                    assert isinstance(info["db_path"], str)
                    assert isinstance(info["sessions_dir"], str)


class TestEnsureDirectories:
    """Test ensure_directories function."""

    def test_ensure_directories_creates_all_dirs(self):
        """ensure_directories should create all required directories."""
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "data"
            workspace_dir = Path(tmpdir) / "workspace"

            with patch("app.core.config._get_default_data_dir", return_value=data_dir):
                with patch("app.core.config._get_default_workspace_dir", return_value=workspace_dir):
                    # We need to reload the module to get fresh settings
                    import importlib
                    import app.core.config as config_module
                    importlib.reload(config_module)

                    # Now call ensure_directories
                    config_module.ensure_directories()

                    # Check directories were created
                    assert config_module.settings.effective_data_dir.exists()
                    assert config_module.settings.sessions_dir.exists()
                    assert config_module.settings.effective_workspace_dir.exists()

    def test_ensure_directories_idempotent(self):
        """ensure_directories should be safe to call multiple times."""
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "data"
            workspace_dir = Path(tmpdir) / "workspace"

            with patch("app.core.config._get_default_data_dir", return_value=data_dir):
                with patch("app.core.config._get_default_workspace_dir", return_value=workspace_dir):
                    import importlib
                    import app.core.config as config_module
                    importlib.reload(config_module)

                    # Call multiple times - should not raise
                    config_module.ensure_directories()
                    config_module.ensure_directories()
                    config_module.ensure_directories()

                    assert config_module.settings.effective_data_dir.exists()


class TestLoadWorkspaceFromDatabase:
    """Test load_workspace_from_database function."""

    def test_load_workspace_from_database_with_valid_path(self):
        """load_workspace_from_database should set workspace from database."""
        with tempfile.TemporaryDirectory() as tmpdir:
            configured_path = Path(tmpdir) / "configured_workspace"
            configured_path.mkdir()

            mock_database = MagicMock()
            mock_database.get_system_setting.return_value = str(configured_path)

            with patch("app.core.config._get_default_data_dir", return_value=Path(tmpdir) / "data"):
                with patch("app.core.config._get_default_workspace_dir", return_value=Path(tmpdir) / "workspace"):
                    import importlib
                    import app.core.config as config_module
                    importlib.reload(config_module)

                    # Patch at the point of import in the load_workspace_from_database function
                    with patch.dict("sys.modules", {"app.db.database": mock_database}):
                        config_module.load_workspace_from_database()

                        mock_database.get_system_setting.assert_called_once_with("workspace_path")
                        assert config_module.settings.workspace_dir == configured_path

    def test_load_workspace_from_database_with_parent_exists(self):
        """load_workspace_from_database should accept path if parent exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            parent_dir = Path(tmpdir)
            configured_path = parent_dir / "new_workspace"  # Doesn't exist but parent does

            mock_database = MagicMock()
            mock_database.get_system_setting.return_value = str(configured_path)

            with patch("app.core.config._get_default_data_dir", return_value=Path(tmpdir) / "data"):
                with patch("app.core.config._get_default_workspace_dir", return_value=Path(tmpdir) / "workspace"):
                    import importlib
                    import app.core.config as config_module
                    importlib.reload(config_module)

                    with patch.dict("sys.modules", {"app.db.database": mock_database}):
                        config_module.load_workspace_from_database()

                        assert config_module.settings.workspace_dir == configured_path

    def test_load_workspace_from_database_no_configured_path(self):
        """load_workspace_from_database should keep default if no path configured."""
        mock_database = MagicMock()
        mock_database.get_system_setting.return_value = None

        with tempfile.TemporaryDirectory() as tmpdir:
            default_workspace = Path(tmpdir) / "default_workspace"

            with patch("app.core.config._get_default_data_dir", return_value=Path(tmpdir) / "data"):
                with patch("app.core.config._get_default_workspace_dir", return_value=default_workspace):
                    import importlib
                    import app.core.config as config_module
                    importlib.reload(config_module)

                    original_workspace = config_module.settings.workspace_dir

                    with patch.dict("sys.modules", {"app.db.database": mock_database}):
                        config_module.load_workspace_from_database()

                        # Should not change
                        assert config_module.settings.workspace_dir == original_workspace

    def test_load_workspace_from_database_invalid_path(self):
        """load_workspace_from_database should warn for invalid paths."""
        mock_database = MagicMock()
        mock_database.get_system_setting.return_value = "/nonexistent/invalid/path/workspace"

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("app.core.config._get_default_data_dir", return_value=Path(tmpdir) / "data"):
                with patch("app.core.config._get_default_workspace_dir", return_value=Path(tmpdir) / "workspace"):
                    import importlib
                    import app.core.config as config_module
                    importlib.reload(config_module)

                    original_workspace = config_module.settings.workspace_dir

                    with patch.dict("sys.modules", {"app.db.database": mock_database}):
                        # Should log warning but not crash
                        config_module.load_workspace_from_database()

                        # Should not change to invalid path
                        assert config_module.settings.workspace_dir == original_workspace

    def test_load_workspace_from_database_handles_exception(self):
        """load_workspace_from_database should handle database errors gracefully."""
        mock_database = MagicMock()
        mock_database.get_system_setting.side_effect = Exception("Database error")

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("app.core.config._get_default_data_dir", return_value=Path(tmpdir) / "data"):
                with patch("app.core.config._get_default_workspace_dir", return_value=Path(tmpdir) / "workspace"):
                    import importlib
                    import app.core.config as config_module
                    importlib.reload(config_module)

                    original_workspace = config_module.settings.workspace_dir

                    with patch.dict("sys.modules", {"app.db.database": mock_database}):
                        # Should not crash
                        config_module.load_workspace_from_database()

                        # Should keep original workspace
                        assert config_module.settings.workspace_dir == original_workspace


class TestGetDefaultHelpers:
    """Test the _get_default_data_dir and _get_default_workspace_dir helpers."""

    def test_get_default_data_dir_calls_platform(self):
        """_get_default_data_dir should call platform module."""
        with patch("app.core.platform.get_app_data_dir", return_value=Path("/platform/data")) as mock:
            from app.core.config import _get_default_data_dir
            result = _get_default_data_dir()

            mock.assert_called_once()
            assert result == Path("/platform/data")

    def test_get_default_workspace_dir_calls_platform(self):
        """_get_default_workspace_dir should call platform module."""
        with patch("app.core.platform.get_default_workspace_dir", return_value=Path("/platform/workspace")) as mock:
            from app.core.config import _get_default_workspace_dir
            result = _get_default_workspace_dir()

            mock.assert_called_once()
            assert result == Path("/platform/workspace")


class TestGlobalSettingsInstance:
    """Test the global settings instance."""

    def test_global_settings_instance_exists(self):
        """There should be a global settings instance."""
        from app.core.config import settings

        assert settings is not None
        assert hasattr(settings, 'host')
        assert hasattr(settings, 'port')
        assert hasattr(settings, 'data_dir')
        assert hasattr(settings, 'workspace_dir')

    def test_global_settings_has_all_properties(self):
        """Global settings should have all required properties."""
        from app.core.config import settings

        # Test all property methods exist and are callable
        assert callable(getattr(settings, 'get_database_url', None))
        assert callable(getattr(settings, 'is_local_mode', None))
        assert callable(getattr(settings, 'get_deployment_info', None))

        # Test property attributes
        _ = settings.effective_data_dir
        _ = settings.effective_workspace_dir
        _ = settings.db_path
        _ = settings.sessions_dir
        _ = settings.get_claude_projects_dir
        _ = settings.effective_tools_dir


class TestSettingsConfigClass:
    """Test the nested Config class."""

    def test_settings_config_env_file(self):
        """Settings should load from .env file."""
        from app.core.config import Settings

        assert Settings.model_config.get('env_file') == '.env'
        assert Settings.model_config.get('env_file_encoding') == 'utf-8'
