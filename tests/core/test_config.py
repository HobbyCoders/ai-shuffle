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
def isolate_data_directory_default(monkeypatch):
    # Tests of Settings defaults must not inherit the test runner's explicit
    # disposable DATA_DIR. The already-created app singleton stays isolated.
    monkeypatch.delenv("DATA_DIR", raising=False)


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

                assert settings.service_name == "ai-shuffle"
                assert settings.version == "0.8.5"

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
                    assert settings.postgres_host is None
                    assert settings.postgres_password is None


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

    def test_get_database_url_requires_configuration(self):
        """get_database_url raises when neither the POSTGRES_* pieces nor a
        legacy DATABASE_URL are set — the app is Postgres-only and there is no
        insecure default."""
        # Clear the deployment-leaked values so the unconfigured path runs.
        # _env_file=None also blocks pydantic-settings from re-reading a
        # repo-root .env — popping the env vars alone is not hermetic.
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("POSTGRES_PASSWORD", None)
            os.environ.pop("DATABASE_URL", None)
            os.environ.pop("POSTGRES_HOST", None)
            with patch("app.core.config._get_default_data_dir", return_value=Path("/mock/data")):
                with patch("app.core.config._get_default_workspace_dir", return_value=Path("/mock/workspace")):
                    from app.core.config import Settings
                    settings = Settings(_env_file=None)

                    assert settings.postgres_password is None
                    assert settings.database_url is None
                    with pytest.raises(ValueError, match="POSTGRES_HOST"):
                        settings.get_database_url()

    def test_get_database_url_legacy_database_url_fallback(self):
        """A legacy DATABASE_URL from a pre-split install keeps working
        verbatim — upgraded installs have no way to re-run setup."""
        with patch.dict(os.environ, {
            "DATABASE_URL": "postgresql://REDACTED@10.0.0.9:5433/olddb",
        }, clear=False):
            os.environ.pop("POSTGRES_PASSWORD", None)
            with patch("app.core.config._get_default_data_dir", return_value=Path("/mock/data")):
                with patch("app.core.config._get_default_workspace_dir", return_value=Path("/mock/workspace")):
                    from app.core.config import Settings
                    settings = Settings(_env_file=None)

                    assert settings.get_database_url() == (
                        "postgresql://REDACTED@10.0.0.9:5433/olddb"
                    )

    def test_get_database_url_legacy_url_wins_over_components(self):
        """When BOTH are configured the legacy DATABASE_URL wins: it captures
        the user's complete previous config (custom user/db/host) that the
        POSTGRES_* defaults would get wrong. A boot warning flags the
        shadowing (see app/core/startup.py)."""
        with patch.dict(os.environ, {
            "DATABASE_URL": "postgresql://REDACTED@external-db:5432/legacydb",
            "POSTGRES_PASSWORD": "component-pw",
            "POSTGRES_HOST": "postgres",
        }, clear=False):
            with patch("app.core.config._get_default_data_dir", return_value=Path("/mock/data")):
                with patch("app.core.config._get_default_workspace_dir", return_value=Path("/mock/workspace")):
                    from app.core.config import Settings
                    settings = Settings(_env_file=None)

                    assert settings.get_database_url() == (
                        "postgresql://REDACTED@external-db:5432/legacydb"
                    )

    def test_get_database_url_built_from_components(self):
        """get_database_url assembles the URL from the POSTGRES_* pieces."""
        with patch.dict(os.environ, {
            "POSTGRES_HOST": "10.0.0.5",
            "POSTGRES_PORT": "5433",
            "POSTGRES_USER": "alice",
            "POSTGRES_PASSWORD": "secret",
            "POSTGRES_DB": "mydb",
        }, clear=False):
            os.environ.pop("DATABASE_URL", None)
            with patch("app.core.config._get_default_data_dir", return_value=Path("/mock/data")):
                with patch("app.core.config._get_default_workspace_dir", return_value=Path("/mock/workspace")):
                    from app.core.config import Settings
                    settings = Settings(_env_file=None)

                    assert settings.get_database_url() == (
                        "postgresql://REDACTED@10.0.0.5:5433/mydb"
                    )

    def test_get_database_url_percent_encodes_password(self):
        """Passwords with URL-special characters survive the round-trip through
        parse_database_url."""
        with patch.dict(os.environ, {
            "POSTGRES_HOST": "localhost",
            "POSTGRES_PASSWORD": "p@ss/w%rd",
        }, clear=False):
            os.environ.pop("DATABASE_URL", None)
            with patch("app.core.config._get_default_data_dir", return_value=Path("/mock/data")):
                with patch("app.core.config._get_default_workspace_dir", return_value=Path("/mock/workspace")):
                    from app.core.config import Settings
                    from app.db.base import parse_database_url
                    settings = Settings(_env_file=None)

                    parsed = parse_database_url(settings.get_database_url())
                    assert parsed["password"] == "p@ss/w%rd"
                    assert parsed["host"] == "localhost"

    def test_get_database_url_default_hosts(self):
        """Host defaults to localhost in local mode and postgres in Docker."""
        with patch.dict(os.environ, {"POSTGRES_PASSWORD": "pw"}, clear=False):
            os.environ.pop("POSTGRES_HOST", None)
            # A deployment-leaked DATABASE_URL would be auto-imported into
            # POSTGRES_HOST at load, defeating the default-host assertion.
            os.environ.pop("DATABASE_URL", None)
            with patch("app.core.config._get_default_data_dir", return_value=Path("/mock/data")):
                with patch("app.core.config._get_default_workspace_dir", return_value=Path("/mock/workspace")):
                    from app.core.config import Settings
                    local = Settings(_env_file=None, local_mode=True)
                    docker = Settings(_env_file=None, local_mode=False)

                    assert local.effective_postgres_host == "localhost"
                    assert docker.effective_postgres_host == "postgres"


class TestLegacyDatabaseUrlAutoImport:
    """A legacy DATABASE_URL is auto-imported into the POSTGRES_* fields at
    settings load, so a URL-only install (pre-split .env or Docker env) counts
    as fully configured without re-running setup."""

    def _make_settings(self, **env):
        import app.core.config as config_mod

        with patch.dict(os.environ, env, clear=False):
            for key in ("POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_USER",
                        "POSTGRES_PASSWORD", "POSTGRES_DB", "DATABASE_URL"):
                if key not in env:
                    os.environ.pop(key, None)
            # Reset the module-global import flag so assertions on it don't
            # see a True left over from an earlier test's URL-bearing env.
            old_flag = config_mod.legacy_database_url_imported
            config_mod.legacy_database_url_imported = False
            try:
                with patch("app.core.config._get_default_data_dir", return_value=Path("/mock/data")):
                    with patch("app.core.config._get_default_workspace_dir", return_value=Path("/mock/workspace")):
                        from app.core.config import Settings
                        result = Settings(_env_file=None)
            finally:
                imported_now = config_mod.legacy_database_url_imported
                config_mod.legacy_database_url_imported = old_flag
        return result, imported_now

    def test_url_only_install_becomes_fully_configured(self):
        """The pre-split .env case: only DATABASE_URL set — every POSTGRES_*
        piece is derived from it and get_database_url() works unchanged."""
        settings, imported = self._make_settings(
            DATABASE_URL="postgresql://REDACTED@10.0.0.9:5433/olddb",
        )
        assert imported is True
        assert settings.postgres_host == "10.0.0.9"
        assert settings.postgres_port == 5433
        assert settings.postgres_user == "olduser"
        assert settings.postgres_password == "oldpass"
        assert settings.postgres_db == "olddb"
        # The URL still wins at connect time, byte-for-byte.
        assert settings.get_database_url() == (
            "postgresql://REDACTED@10.0.0.9:5433/olddb"
        )

    def test_percent_encoded_and_literal_at_passwords_round_trip(self):
        """The raw-DSN trap: passwords with % or @ survive the import."""
        settings, _ = self._make_settings(
            DATABASE_URL="postgresql://REDACTED@10.0.0.200:5432/appdb",
        )
        assert settings.postgres_password == "p@ss"

        settings, _ = self._make_settings(
            DATABASE_URL="postgresql://REDACTED@10.0.0.200:5432/appdb",
        )
        assert settings.postgres_password == "p@ssPwd"

    def test_explicit_pieces_are_never_clobbered(self):
        """An explicit POSTGRES_* value beats the URL's counterpart — a user
        overriding one piece isn't silently reverted by the import."""
        settings, _ = self._make_settings(
            DATABASE_URL="postgresql://REDACTED@10.0.0.9:5433/olddb",
            POSTGRES_PASSWORD="explicit-pw",
        )
        assert settings.postgres_password == "explicit-pw"
        # Untouched pieces still come from the URL.
        assert settings.postgres_host == "10.0.0.9"
        assert settings.postgres_db == "olddb"

    def test_no_url_leaves_defaults(self):
        """No DATABASE_URL — nothing changes and the flag stays False."""
        settings, imported = self._make_settings(POSTGRES_PASSWORD="pw")
        assert imported is False
        assert settings.postgres_host is None
        assert settings.postgres_db == "ai_shuffle"



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

    def test_settings_normalizes_at_load(self):
        """The rewrite happens once, at load, so every consumer of
        ``get_database_url()`` sees the encoded form."""
        with patch.dict(os.environ, {
            "DATABASE_URL": "postgresql://REDACTED@10.0.0.200:5432/ai_shuffle",
        }, clear=False):
            os.environ.pop("POSTGRES_PASSWORD", None)
            with patch("app.core.config._get_default_data_dir", return_value=Path("/mock/data")):
                with patch("app.core.config._get_default_workspace_dir", return_value=Path("/mock/workspace")):
                    from app.core.config import Settings
                    settings = Settings(_env_file=None)

                    assert settings.database_url == (
                        "postgresql://REDACTED@10.0.0.200:5432/ai_shuffle"
                    )
                    assert settings.get_database_url() == settings.database_url

    def test_normalized_url_parses_back_to_the_real_password(self):
        """The point of the exercise: the credentials still decode correctly,
        and the host is the host."""
        with patch.dict(os.environ, {
            "DATABASE_URL": "postgresql://REDACTED@10.0.0.200:5432/ai_shuffle",
        }, clear=False):
            os.environ.pop("POSTGRES_PASSWORD", None)
            with patch("app.core.config._get_default_data_dir", return_value=Path("/mock/data")):
                with patch("app.core.config._get_default_workspace_dir", return_value=Path("/mock/workspace")):
                    from app.core.config import Settings
                    from app.db.base import parse_database_url
                    settings = Settings(_env_file=None)

                    parsed = parse_database_url(settings.get_database_url())
                    assert parsed["host"] == "10.0.0.200"
                    assert parsed["password"] == "Pwd@word"
                    assert parsed["user"] == "ai_shuffle"
                    # And the POSTGRES_* import took the same decoded values.
                    assert settings.postgres_host == "10.0.0.200"
                    assert settings.postgres_password == "Pwd@word"

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
