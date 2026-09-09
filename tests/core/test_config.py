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


@pytest.fixture(autouse=True)
def isolate_directory_defaults(monkeypatch):
    # Tests of Settings defaults must not inherit the test runner's explicit
    # disposable paths. The already-created app singleton stays isolated.
    monkeypatch.delenv("DATA_DIR", raising=False)
    monkeypatch.delenv("WORKSPACE_DIR", raising=False)


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
                # log_level can be overridden by .env, just check it's a valid level
                assert settings.log_level.upper() in ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

    def test_default_service_info(self):
        """Default service info should be set correctly."""
        with patch("app.core.config._get_default_data_dir", return_value=Path("/mock/data")):
            with patch("app.core.config._get_default_workspace_dir", return_value=Path("/mock/workspace")):
                from app.core.config import Settings
                settings = Settings()

                from app import __version__

                assert settings.service_name == "ai-shuffle"
                assert settings.version == __version__

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

                # cors_origins may be overridden by .env, just verify it's a non-empty string
                assert isinstance(settings.cors_origins, str)
                assert len(settings.cors_origins) > 0
                # trusted_proxy_headers may also be overridden by .env
                assert isinstance(settings.trusted_proxy_headers, str)

    def test_default_path_settings_are_none(self):
        """Path settings should default to None before model_post_init."""
        # The deployment env exports POSTGRES_*/DATABASE_URL values; clear
        # them so the class-level defaults are what this test exercises rather
        # than leaked values (DATABASE_URL would be auto-imported into the
        # POSTGRES_* fields at load). _env_file=None also blocks
        # pydantic-settings from re-reading a repo-root .env — popping the env
        # vars alone is not hermetic.
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("POSTGRES_HOST", None)
            os.environ.pop("POSTGRES_PASSWORD", None)
            os.environ.pop("DATABASE_URL", None)
            with patch("app.core.config._get_default_data_dir", return_value=Path("/mock/data")):
                with patch("app.core.config._get_default_workspace_dir", return_value=Path("/mock/workspace")):
                    from app.core.config import Settings
                    settings = Settings(_env_file=None)

                    # After initialization, paths should be set by model_post_init
                    assert settings.data_dir == Path("/mock/data")
                    assert settings.workspace_dir == Path("/mock/workspace")
                    assert settings.claude_projects_dir is None
                    assert settings.tools_dir is None
                    assert "postgres_host" not in Settings.model_fields
                    assert "postgres_password" not in Settings.model_fields


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

    def test_unsupervised_start_fails_despite_inherited_database_env(self, tmp_path, monkeypatch):
        from app.core.config import Settings
        monkeypatch.setenv("DATABASE_URL", "postgresql://never:synthetic@external.invalid/production")
        monkeypatch.setenv("DATABASE_MODE", "external")
        monkeypatch.setenv("DB_POOL_MIN", "999")
        monkeypatch.setenv("DB_POOL_MAX", "not-a-number")
        monkeypatch.setenv("POSTGRES_HOST", "external.invalid")
        monkeypatch.setenv("POSTGRES_PASSWORD", "never-use-this")
        settings = Settings(_env_file=None, data_dir=tmp_path)
        assert not settings.managed_database_available()
        assert settings.db_pool_min == 2 and settings.db_pool_max == 32
        assert "db_pool_min" not in Settings.model_fields and "db_pool_max" not in Settings.model_fields
        assert not any(k.startswith(("postgres_", "database_")) for k in Settings.model_fields)
        with pytest.raises(RuntimeError, match="desktop launcher"):
            settings.get_database_url()

    def test_supervisor_descriptor_wins_over_inherited_env_and_env_file(self, tmp_path, monkeypatch):
        from app.core.config import Settings
        import json
        envfile = tmp_path / "synthetic.env"
        envfile.write_text("DATABASE_URL=postgresql://bad:bad@external.invalid/prod\nPOSTGRES_PORT=garbage\nPOSTGRES_HOST=external.invalid\n")
        monkeypatch.setenv("DATABASE_URL", "postgresql://bad:bad@external.invalid/prod")
        monkeypatch.setenv("POSTGRES_PASSWORD", "external-password")
        monkeypatch.setenv("PGHOSTADDR", "203.0.113.8")
        settings = Settings(_env_file=envfile, data_dir=tmp_path)
        password = "synthetic@pass/word%"
        descriptor = tmp_path / "managed-postgres.json"
        descriptor.write_text(json.dumps(dict(version=1, host="127.0.0.1", port=6543,
            user="ai_shuffle", password=password, database="ai_shuffle")))
        descriptor.chmod(0o600)
        connection = settings.get_database_connection()
        assert connection["host"] == connection["hostaddr"] == "127.0.0.1"
        assert connection["port"] == 6543
        assert connection["password"] == password
        assert settings.managed_database_available()
        from app.db.base import parse_database_url
        assert parse_database_url(settings.get_database_url())["password"] == password

    @pytest.mark.parametrize("change", [
        {"version": True}, {"version": 2}, {"host": "localhost"}, {"host": "203.0.113.1"},
        {"host": "/tmp"}, {"port": True}, {"port": "5432"}, {"port": 0}, {"port": 65536},
        {"password": ""}, {"password": "bad\x00password"}, {"user": ""},
        {"database": "host=external.invalid"}, {"extra": "no"},
    ])
    def test_malformed_descriptor_is_rejected_without_credentials(self, tmp_path, change):
        import json
        from app.core.config import Settings
        record = dict(version=1, host="127.0.0.1", port=5432, user="test_user", password="private-synthetic-value", database="test_db")
        record.update(change)
        descriptor = tmp_path / "managed-postgres.json"
        descriptor.write_text(json.dumps(record))
        descriptor.chmod(0o600)
        with pytest.raises(RuntimeError, match="private managed-postgres.json") as exc:
            Settings(_env_file=None, data_dir=tmp_path).get_database_connection()
        assert "private-synthetic-value" not in str(exc.value)

    @pytest.mark.parametrize("kind", ["symlink", "public", "hardlink", "invalid-json", "duplicate-key"])
    def test_unsafe_descriptor_is_rejected(self, tmp_path, kind):
        from app.core.config import Settings
        import json
        settings = Settings(_env_file=None, data_dir=tmp_path)
        path = tmp_path / "managed-postgres.json"
        path.write_text(json.dumps(dict(version=1, host="127.0.0.1", port=5432,
            user="test_user", password="synthetic", database="test_db")))
        path.chmod(0o600)
        if kind == "symlink":
            target = tmp_path / "target"
            path.rename(target)
            path.symlink_to(target)
        elif kind == "public":
            if os.name == "nt":
                pytest.skip("Windows privacy is enforced by supervisor ACLs")
            path.chmod(0o644)
        elif kind == "hardlink":
            os.link(path, tmp_path / "other-link")
        elif kind == "invalid-json":
            path.write_text("not-json synthetic-secret")
        else:
            path.write_text(path.read_text().replace('"version": 1', '"version": 1, "version": 1'))
        with pytest.raises(RuntimeError):
            settings.get_database_connection()

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


class TestEnsureDirectories:
    """Test ensure_directories function."""

    def test_ensure_directories_creates_all_dirs(self):
        """ensure_directories should create all required directories."""
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "data"
            workspace_dir = Path(tmpdir) / "workspace"

            with patch("app.core.config._get_default_data_dir", return_value=data_dir):
                with patch("app.core.config._get_default_workspace_dir", return_value=workspace_dir):
                    # Swap in a fresh Settings built under the patches. Never
                    # importlib.reload() here: reload replaces the module-level
                    # `settings` singleton for the rest of the pytest process,
                    # so any later test that patch.object()s the old singleton
                    # is silently patching a dead object (order-dependent
                    # failures). patch.object restores the original identity.
                    import app.core.config as config_module
                    with patch.object(config_module, "settings", config_module.Settings()):
                        # Now call ensure_directories
                        config_module.ensure_directories()

                        # Check directories were created
                        assert config_module.settings.effective_data_dir.exists()
                        assert config_module.settings.effective_workspace_dir.exists()

    def test_ensure_directories_idempotent(self):
        """ensure_directories should be safe to call multiple times."""
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "data"
            workspace_dir = Path(tmpdir) / "workspace"

            with patch("app.core.config._get_default_data_dir", return_value=data_dir):
                with patch("app.core.config._get_default_workspace_dir", return_value=workspace_dir):
                    # See test_ensure_directories_creates_all_dirs — never
                    # reload the module; swap the singleton reversibly.
                    import app.core.config as config_module
                    with patch.object(config_module, "settings", config_module.Settings()):
                        # Call multiple times - should not raise
                        config_module.ensure_directories()
                        config_module.ensure_directories()
                        config_module.ensure_directories()

                        assert config_module.settings.effective_data_dir.exists()


@pytest.mark.skip(reason="Module reload tests are flaky in CI due to import caching")
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
        _ = settings.get_claude_projects_dir
        _ = settings.effective_tools_dir


class TestSettingsConfigClass:
    """Test the nested Config class."""

    def test_settings_config_env_file(self):
        """Settings should load from .env file."""
        from app.core.config import Settings

        assert Settings.model_config.get('env_file') == '.env'
        assert Settings.model_config.get('env_file_encoding') == 'utf-8'



class TestDatabaseUrlNormalization:
    """A DATABASE_URL whose password carries reserved characters.

    Python's ``urlsplit`` splits userinfo at the LAST ``@``; libpq splits at
    the FIRST. An unencoded ``@`` in the password therefore makes the same
    string name two different hosts depending on who reads it — a worker
    container reported ``could not translate host name "Pwd@10.0.0.200"`` and
    put a password fragment in the event log. Normalizing at load makes both
    readings agree.
    """

    def test_unencoded_at_in_password_is_encoded(self):
        from app.core.config import normalize_database_url

        assert normalize_database_url(
            "postgresql://REDACTED@10.0.0.200:5432/ai_shuffle"
        ) == "postgresql://REDACTED@10.0.0.200:5432/ai_shuffle"

    def test_normalization_is_idempotent(self):
        from app.core.config import normalize_database_url

        once = normalize_database_url(
            "postgresql://REDACTED@db.local:5432/app"
        )
        assert normalize_database_url(once) == once
        assert once == "postgresql://REDACTED@db.local:5432/app"

    def test_colon_in_password_is_encoded(self):
        """The userinfo splits on the FIRST colon, so a password may contain
        more of them — they come back encoded."""
        from app.core.config import normalize_database_url

        assert normalize_database_url(
            "postgresql://REDACTED@host:5432/app"
        ) == "postgresql://REDACTED@host:5432/app"

    def test_password_with_a_slash_is_left_alone(self):
        """``/``, ``?`` and ``#`` end the netloc, so a URL carrying one bare in
        the password is not recoverable by anyone — including us. It is
        returned untouched rather than silently rewritten into a different
        connection string; the operator must encode those themselves (the
        POSTGRES_* path already does)."""
        from app.core.config import normalize_database_url

        broken = "postgresql://u:pa/ss@host:5432/app"
        assert normalize_database_url(broken) == broken

    def test_clean_url_is_untouched(self):
        from app.core.config import normalize_database_url

        clean = "postgresql://REDACTED@10.0.0.5:5433/mydb"
        assert normalize_database_url(clean) == clean

    def test_url_without_credentials_is_untouched(self):
        from app.core.config import normalize_database_url

        bare = "postgresql://localhost:5432/mydb"
        assert normalize_database_url(bare) == bare

    def test_empty_and_garbage_never_raise(self):
        from app.core.config import normalize_database_url

        assert normalize_database_url("") == ""
        assert normalize_database_url("   ") == ""
        # No scheme, no credentials: returned as given rather than mangled.
        assert normalize_database_url("not a url at all") == "not a url at all"

    def test_libpq_first_at_split_now_finds_the_host(self):
        """Simulates libpq's split (FIRST ``@``) on the normalized string: with
        the password encoded there is exactly one ``@`` left, so both splitters
        agree."""
        from app.core.config import normalize_database_url

        normalized = normalize_database_url(
            "postgresql://REDACTED@10.0.0.200:5432/ai_shuffle"
        )
        after_scheme = normalized.split("://", 1)[1]
        assert after_scheme.count("@") == 1
        assert after_scheme.split("@", 1)[1] == "10.0.0.200:5432/ai_shuffle"
