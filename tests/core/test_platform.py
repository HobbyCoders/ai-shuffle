"""
Unit tests for platform detection and OS-specific path handling module.

Tests cover:
- DeploymentMode enum
- Deployment mode detection (LOCAL_MODE env, Docker indicators)
- App data directory paths for all platforms
- Default workspace directory paths for all platforms
- Claude credentials directory
- Claude CLI executable detection
- GitHub CLI executable detection
- Helper functions (is_local_mode, is_docker_mode)
- Platform info gathering
"""

import os
import sys
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from app.core.platform import (
    DeploymentMode,
    detect_deployment_mode,
    get_app_data_dir,
    get_default_workspace_dir,
    get_claude_credentials_dir,
    get_claude_executable,
    get_gh_executable,
    is_local_mode,
    is_docker_mode,
    get_platform_info,
)


class TestDeploymentMode:
    """Test DeploymentMode enum."""

    def test_docker_value(self):
        """DOCKER mode should have value 'docker'."""
        assert DeploymentMode.DOCKER.value == "docker"

    def test_local_value(self):
        """LOCAL mode should have value 'local'."""
        assert DeploymentMode.LOCAL.value == "local"

    def test_enum_members(self):
        """Enum should have exactly two members."""
        members = list(DeploymentMode)
        assert len(members) == 2
        assert DeploymentMode.DOCKER in members
        assert DeploymentMode.LOCAL in members


class TestDetectDeploymentModeEnvOverride:
    """Test deployment mode detection via LOCAL_MODE environment variable."""

    def test_local_mode_true(self):
        """LOCAL_MODE=true should return LOCAL."""
        with patch.dict(os.environ, {"LOCAL_MODE": "true"}, clear=False):
            assert detect_deployment_mode() == DeploymentMode.LOCAL

    def test_local_mode_1(self):
        """LOCAL_MODE=1 should return LOCAL."""
        with patch.dict(os.environ, {"LOCAL_MODE": "1"}, clear=False):
            assert detect_deployment_mode() == DeploymentMode.LOCAL

    def test_local_mode_yes(self):
        """LOCAL_MODE=yes should return LOCAL."""
        with patch.dict(os.environ, {"LOCAL_MODE": "yes"}, clear=False):
            assert detect_deployment_mode() == DeploymentMode.LOCAL

    def test_local_mode_TRUE_uppercase(self):
        """LOCAL_MODE=TRUE (uppercase) should return LOCAL."""
        with patch.dict(os.environ, {"LOCAL_MODE": "TRUE"}, clear=False):
            assert detect_deployment_mode() == DeploymentMode.LOCAL

    def test_local_mode_false(self):
        """LOCAL_MODE=false should return DOCKER."""
        with patch.dict(os.environ, {"LOCAL_MODE": "false"}, clear=False):
            assert detect_deployment_mode() == DeploymentMode.DOCKER

    def test_local_mode_0(self):
        """LOCAL_MODE=0 should return DOCKER."""
        with patch.dict(os.environ, {"LOCAL_MODE": "0"}, clear=False):
            assert detect_deployment_mode() == DeploymentMode.DOCKER

    def test_local_mode_no(self):
        """LOCAL_MODE=no should return DOCKER."""
        with patch.dict(os.environ, {"LOCAL_MODE": "no"}, clear=False):
            assert detect_deployment_mode() == DeploymentMode.DOCKER


class TestDetectDeploymentModeDockerIndicators:
    """Test deployment mode detection via Docker indicators."""

    @pytest.fixture(autouse=True)
    def clear_local_mode_env(self):
        """Ensure LOCAL_MODE is not set for these tests."""
        env_copy = os.environ.copy()
        if "LOCAL_MODE" in os.environ:
            del os.environ["LOCAL_MODE"]
        yield
        os.environ.clear()
        os.environ.update(env_copy)

    def test_dockerenv_file_exists(self):
        """/.dockerenv file should indicate DOCKER mode."""
        with patch.object(Path, "exists") as mock_exists:
            mock_exists.return_value = True
            assert detect_deployment_mode() == DeploymentMode.DOCKER

    def test_user_appuser(self):
        """USER=appuser should indicate DOCKER mode."""
        with patch.object(Path, "exists", return_value=False):
            with patch.dict(os.environ, {"USER": "appuser"}, clear=False):
                assert detect_deployment_mode() == DeploymentMode.DOCKER

    def test_home_appuser(self):
        """HOME=/home/appuser should indicate DOCKER mode."""
        with patch.object(Path, "exists", return_value=False):
            # Remove USER if present to avoid triggering that condition first
            env = {"HOME": "/home/appuser"}
            with patch.dict(os.environ, env, clear=False):
                # Clear USER if it was set
                if "USER" in os.environ:
                    del os.environ["USER"]
                assert detect_deployment_mode() == DeploymentMode.DOCKER

    def test_docker_volume_mounts(self):
        """Presence of /data, /workspace, and /app should indicate DOCKER mode."""
        # Clear Docker user indicators
        with patch.dict(os.environ, {"USER": "testuser", "HOME": "/home/testuser"}, clear=False):
            # Mock Path to return controlled behavior
            mock_data = MagicMock(spec=Path)
            mock_data.exists.return_value = True

            mock_workspace = MagicMock(spec=Path)
            mock_workspace.exists.return_value = True

            mock_app = MagicMock(spec=Path)
            mock_app.exists.return_value = True

            mock_dockerenv = MagicMock(spec=Path)
            mock_dockerenv.exists.return_value = False

            # Patch Path constructor to return our mocks
            original_path = Path

            def mock_path_init(path_str):
                if path_str == "/.dockerenv":
                    return mock_dockerenv
                elif path_str == "/data":
                    return mock_data
                elif path_str == "/workspace":
                    return mock_workspace
                elif path_str == "/app":
                    return mock_app
                return original_path(path_str)

            with patch("app.core.platform.Path", side_effect=mock_path_init):
                assert detect_deployment_mode() == DeploymentMode.DOCKER

    def test_default_to_local_no_docker_indicators(self):
        """Should default to LOCAL when no Docker indicators found."""
        with patch.dict(os.environ, {"USER": "testuser", "HOME": "/home/testuser"}, clear=False):
            with patch.object(Path, "exists", return_value=False):
                assert detect_deployment_mode() == DeploymentMode.LOCAL


class TestGetAppDataDir:
    """Test app data directory path generation."""

    @pytest.fixture(autouse=True)
    def mock_local_mode(self):
        """Set LOCAL_MODE=true to avoid Docker detection complexity."""
        with patch.dict(os.environ, {"LOCAL_MODE": "true"}, clear=False):
            yield

    def test_docker_mode_returns_data(self):
        """Docker mode should return /data."""
        with patch.dict(os.environ, {"LOCAL_MODE": "false"}, clear=False):
            result = get_app_data_dir()
            assert result == Path("/data")

    def test_windows_with_userprofile(self):
        """Windows with USERPROFILE should use %USERPROFILE%/.ai-shuffle."""
        with patch("app.core.platform.sys") as mock_sys:
            mock_sys.platform = "win32"
            with patch.dict(os.environ, {"USERPROFILE": "C:\\Users\\TestUser"}, clear=False):
                result = get_app_data_dir()
                assert result == Path("C:\\Users\\TestUser/.ai-shuffle")

    def test_windows_without_userprofile(self):
        """Windows without USERPROFILE should fall back to home directory."""
        with patch("app.core.platform.sys") as mock_sys:
            mock_sys.platform = "win32"
            with patch.dict(os.environ, {}, clear=False):
                if "USERPROFILE" in os.environ:
                    del os.environ["USERPROFILE"]
                with patch.object(Path, "home", return_value=Path("C:/Users/FallbackUser")):
                    result = get_app_data_dir()
                    assert result == Path("C:/Users/FallbackUser/.ai-shuffle")

    def test_macos_uses_application_support(self):
        """macOS should use ~/Library/Application Support/ai-shuffle."""
        with patch("app.core.platform.sys") as mock_sys:
            mock_sys.platform = "darwin"
            with patch.object(Path, "home", return_value=Path("/Users/testuser")):
                result = get_app_data_dir()
                assert result == Path("/Users/testuser/Library/Application Support/ai-shuffle")

    def test_linux_with_xdg_data_home(self):
        """Linux with XDG_DATA_HOME should use that directory."""
        with patch("app.core.platform.sys") as mock_sys:
            mock_sys.platform = "linux"
            with patch.dict(os.environ, {"XDG_DATA_HOME": "/custom/data"}, clear=False):
                result = get_app_data_dir()
                assert result == Path("/custom/data/ai-shuffle")

    def test_linux_without_xdg_data_home(self):
        """Linux without XDG_DATA_HOME should use ~/.local/share/ai-shuffle."""
        with patch("app.core.platform.sys") as mock_sys:
            mock_sys.platform = "linux"
            with patch.dict(os.environ, {}, clear=False):
                if "XDG_DATA_HOME" in os.environ:
                    del os.environ["XDG_DATA_HOME"]
                with patch.object(Path, "home", return_value=Path("/home/testuser")):
                    result = get_app_data_dir()
                    assert result == Path("/home/testuser/.local/share/ai-shuffle")


class TestGetDefaultWorkspaceDir:
    """Test default workspace directory path generation."""

    @pytest.fixture(autouse=True)
    def mock_local_mode(self):
        """Set LOCAL_MODE=true to avoid Docker detection complexity."""
        with patch.dict(os.environ, {"LOCAL_MODE": "true"}, clear=False):
            yield

    def test_docker_mode_returns_workspace(self):
        """Docker mode should return /workspace."""
        with patch.dict(os.environ, {"LOCAL_MODE": "false"}, clear=False):
            result = get_default_workspace_dir()
            assert result == Path("/workspace")

    def test_windows_with_userprofile(self):
        """Windows with USERPROFILE should use Documents folder."""
        with patch("app.core.platform.sys") as mock_sys:
            mock_sys.platform = "win32"
            with patch.dict(os.environ, {"USERPROFILE": "C:\\Users\\TestUser"}, clear=False):
                result = get_default_workspace_dir()
                assert result == Path("C:\\Users\\TestUser/Documents/ai-shuffle-workspace")

    def test_windows_without_userprofile(self):
        """Windows without USERPROFILE should fall back to home directory."""
        with patch("app.core.platform.sys") as mock_sys:
            mock_sys.platform = "win32"
            with patch.dict(os.environ, {}, clear=False):
                if "USERPROFILE" in os.environ:
                    del os.environ["USERPROFILE"]
                with patch.object(Path, "home", return_value=Path("C:/Users/FallbackUser")):
                    result = get_default_workspace_dir()
                    assert result == Path("C:/Users/FallbackUser/Documents/ai-shuffle-workspace")

    def test_macos_uses_home_directory(self):
        """macOS should use ~/ai-shuffle-workspace."""
        with patch("app.core.platform.sys") as mock_sys:
            mock_sys.platform = "darwin"
            with patch.object(Path, "home", return_value=Path("/Users/testuser")):
                result = get_default_workspace_dir()
                assert result == Path("/Users/testuser/ai-shuffle-workspace")

    def test_linux_uses_home_directory(self):
        """Linux should use ~/ai-shuffle-workspace."""
        with patch("app.core.platform.sys") as mock_sys:
            mock_sys.platform = "linux"
            with patch.object(Path, "home", return_value=Path("/home/testuser")):
                result = get_default_workspace_dir()
                assert result == Path("/home/testuser/ai-shuffle-workspace")


class TestGetClaudeCredentialsDir:
    """Test Claude credentials directory."""

    def test_returns_home_claude(self):
        """Should return ~/.claude."""
        with patch.object(Path, "home", return_value=Path("/home/testuser")):
            result = get_claude_credentials_dir()
            assert result == Path("/home/testuser/.claude")


class TestGetClaudeExecutable:
    """Test Claude CLI executable detection."""

    def test_found_via_shutil_which_claude(self):
        """Should find 'claude' via shutil.which."""
        with patch("shutil.which") as mock_which:
            mock_which.return_value = "/usr/local/bin/claude"
            result = get_claude_executable()
            assert result == "/usr/local/bin/claude"

    def test_found_via_shutil_which_claude_exe(self):
        """Should find 'claude.exe' via shutil.which."""
        with patch("shutil.which") as mock_which:
            def which_side_effect(name):
                if name == "claude.exe":
                    return "C:\\Program Files\\claude\\claude.exe"
                return None
            mock_which.side_effect = which_side_effect
            result = get_claude_executable()
            assert result == "C:\\Program Files\\claude\\claude.exe"

    def test_found_via_shutil_which_claude_code(self):
        """Should find 'claude-code' via shutil.which."""
        with patch("shutil.which") as mock_which:
            def which_side_effect(name):
                if name == "claude-code":
                    return "/usr/bin/claude-code"
                return None
            mock_which.side_effect = which_side_effect
            result = get_claude_executable()
            assert result == "/usr/bin/claude-code"

    def test_windows_npm_global_install(self):
        """Windows should check npm global install location."""
        def mock_exists(path_self):
            return str(path_self) == "C:\\Users\\Test\\AppData\\Roaming\\npm\\claude.cmd"

        with patch("shutil.which", return_value=None):
            with patch("app.core.platform.sys") as mock_sys:
                mock_sys.platform = "win32"
                with patch.dict(os.environ, {
                    "APPDATA": "C:\\Users\\Test\\AppData\\Roaming",
                    "LOCALAPPDATA": "C:\\Users\\Test\\AppData\\Local"
                }, clear=False):
                    with patch.object(Path, "exists", mock_exists):
                        result = get_claude_executable()
                        assert result == "C:\\Users\\Test\\AppData\\Roaming\\npm\\claude.cmd"

    def test_windows_npm_global_install_no_ext(self):
        """Windows should also check npm/claude without .cmd extension."""
        def mock_exists(path_self):
            return str(path_self) == "C:\\Users\\Test\\AppData\\Roaming\\npm\\claude"

        with patch("shutil.which", return_value=None):
            with patch("app.core.platform.sys") as mock_sys:
                mock_sys.platform = "win32"
                with patch.dict(os.environ, {
                    "APPDATA": "C:\\Users\\Test\\AppData\\Roaming",
                    "LOCALAPPDATA": "C:\\Users\\Test\\AppData\\Local"
                }, clear=False):
                    with patch.object(Path, "exists", mock_exists):
                        result = get_claude_executable()
                        assert result == "C:\\Users\\Test\\AppData\\Roaming\\npm\\claude"

    def test_windows_program_files(self):
        """Windows should check Program Files location."""
        def mock_exists(path_self):
            return str(path_self) == "C:\\Program Files\\nodejs\\claude.cmd"

        with patch("shutil.which", return_value=None):
            with patch("app.core.platform.sys") as mock_sys:
                mock_sys.platform = "win32"
                with patch.dict(os.environ, {
                    "APPDATA": "C:\\Users\\Test\\AppData\\Roaming",
                    "LOCALAPPDATA": "C:\\Users\\Test\\AppData\\Local"
                }, clear=False):
                    with patch.object(Path, "exists", mock_exists):
                        result = get_claude_executable()
                        assert result == "C:\\Program Files\\nodejs\\claude.cmd"

    def test_windows_localappdata_claude(self):
        """Windows should check LOCALAPPDATA for claude.exe."""
        def mock_exists(path_self):
            return str(path_self) == "C:\\Users\\Test\\AppData\\Local\\Programs\\claude\\claude.exe"

        with patch("shutil.which", return_value=None):
            with patch("app.core.platform.sys") as mock_sys:
                mock_sys.platform = "win32"
                with patch.dict(os.environ, {
                    "APPDATA": "C:\\Users\\Test\\AppData\\Roaming",
                    "LOCALAPPDATA": "C:\\Users\\Test\\AppData\\Local"
                }, clear=False):
                    with patch.object(Path, "exists", mock_exists):
                        result = get_claude_executable()
                        assert result == "C:\\Users\\Test\\AppData\\Local\\Programs\\claude\\claude.exe"

    @pytest.mark.skipif(sys.platform == "win32", reason="Unix path tests require non-Windows platform")
    def test_unix_npm_global_bin(self):
        """Unix should check ~/.npm-global/bin."""
        def mock_exists(path_self):
            return str(path_self) == "/home/testuser/.npm-global/bin/claude"

        with patch("shutil.which", return_value=None):
            with patch.object(Path, "home", return_value=Path("/home/testuser")):
                with patch.object(Path, "exists", mock_exists):
                    result = get_claude_executable()
                    assert result == "/home/testuser/.npm-global/bin/claude"

    @pytest.mark.skipif(sys.platform == "win32", reason="Unix path tests require non-Windows platform")
    def test_unix_usr_local_bin(self):
        """Unix should check /usr/local/bin."""
        def mock_exists(path_self):
            return str(path_self) == "/usr/local/bin/claude"

        with patch("shutil.which", return_value=None):
            with patch.object(Path, "home", return_value=Path("/home/testuser")):
                with patch.object(Path, "exists", mock_exists):
                    result = get_claude_executable()
                    assert result == "/usr/local/bin/claude"

    @pytest.mark.skipif(sys.platform == "win32", reason="Unix path tests require non-Windows platform")
    def test_unix_usr_bin(self):
        """Unix should check /usr/bin."""
        def mock_exists(path_self):
            return str(path_self) == "/usr/bin/claude"

        with patch("shutil.which", return_value=None):
            with patch.object(Path, "home", return_value=Path("/home/testuser")):
                with patch.object(Path, "exists", mock_exists):
                    result = get_claude_executable()
                    assert result == "/usr/bin/claude"

    @pytest.mark.skipif(sys.platform == "win32", reason="Unix path tests require non-Windows platform")
    def test_unix_local_bin(self):
        """Unix should check ~/.local/bin."""
        def mock_exists(path_self):
            return str(path_self) == "/home/testuser/.local/bin/claude"

        with patch("shutil.which", return_value=None):
            with patch.object(Path, "home", return_value=Path("/home/testuser")):
                with patch.object(Path, "exists", mock_exists):
                    result = get_claude_executable()
                    assert result == "/home/testuser/.local/bin/claude"

    def test_not_found_returns_none(self):
        """Should return None if claude is not found anywhere."""
        with patch("shutil.which", return_value=None):
            with patch("app.core.platform.sys") as mock_sys:
                mock_sys.platform = "linux"
                with patch.object(Path, "home", return_value=Path("/home/testuser")):
                    with patch.object(Path, "exists", return_value=False):
                        result = get_claude_executable()
                        assert result is None


class TestGetGhExecutable:
    """Test GitHub CLI executable detection."""

    def test_found_via_shutil_which(self):
        """Should find 'gh' via shutil.which."""
        with patch("shutil.which", return_value="/usr/local/bin/gh"):
            result = get_gh_executable()
            assert result == "/usr/local/bin/gh"

    def test_windows_program_files(self):
        """Windows should check Program Files location."""
        def mock_exists(path_self):
            return str(path_self) == "C:\\Program Files\\GitHub CLI\\gh.exe"

        with patch("shutil.which", return_value=None):
            with patch("app.core.platform.sys") as mock_sys:
                mock_sys.platform = "win32"
                with patch.object(Path, "exists", mock_exists):
                    result = get_gh_executable()
                    assert result == "C:\\Program Files\\GitHub CLI\\gh.exe"

    def test_windows_program_files_x86(self):
        """Windows should check Program Files (x86) location."""
        def mock_exists(path_self):
            return str(path_self) == "C:\\Program Files (x86)\\GitHub CLI\\gh.exe"

        with patch("shutil.which", return_value=None):
            with patch("app.core.platform.sys") as mock_sys:
                mock_sys.platform = "win32"
                with patch.object(Path, "exists", mock_exists):
                    result = get_gh_executable()
                    assert result == "C:\\Program Files (x86)\\GitHub CLI\\gh.exe"

    @pytest.mark.skipif(sys.platform == "win32", reason="Unix path tests require non-Windows platform")
    def test_unix_usr_local_bin(self):
        """Unix should check /usr/local/bin."""
        def mock_exists(path_self):
            return str(path_self) == "/usr/local/bin/gh"

        with patch("shutil.which", return_value=None):
            with patch.object(Path, "home", return_value=Path("/home/testuser")):
                with patch.object(Path, "exists", mock_exists):
                    result = get_gh_executable()
                    assert result == "/usr/local/bin/gh"

    @pytest.mark.skipif(sys.platform == "win32", reason="Unix path tests require non-Windows platform")
    def test_unix_usr_bin(self):
        """Unix should check /usr/bin."""
        def mock_exists(path_self):
            return str(path_self) == "/usr/bin/gh"

        with patch("shutil.which", return_value=None):
            with patch.object(Path, "home", return_value=Path("/home/testuser")):
                with patch.object(Path, "exists", mock_exists):
                    result = get_gh_executable()
                    assert result == "/usr/bin/gh"

    @pytest.mark.skipif(sys.platform == "win32", reason="Unix path tests require non-Windows platform")
    def test_unix_local_bin(self):
        """Unix should check ~/.local/bin."""
        def mock_exists(path_self):
            return str(path_self) == "/home/testuser/.local/bin/gh"

        with patch("shutil.which", return_value=None):
            with patch.object(Path, "home", return_value=Path("/home/testuser")):
                with patch.object(Path, "exists", mock_exists):
                    result = get_gh_executable()
                    assert result == "/home/testuser/.local/bin/gh"

    def test_not_found_returns_none(self):
        """Should return None if gh is not found anywhere."""
        with patch("shutil.which", return_value=None):
            with patch("app.core.platform.sys") as mock_sys:
                mock_sys.platform = "linux"
                with patch.object(Path, "home", return_value=Path("/home/testuser")):
                    with patch.object(Path, "exists", return_value=False):
                        result = get_gh_executable()
                        assert result is None


class TestIsLocalMode:
    """Test is_local_mode helper."""

    def test_returns_true_when_local(self):
        """Should return True when in local mode."""
        with patch("app.core.platform.detect_deployment_mode", return_value=DeploymentMode.LOCAL):
            assert is_local_mode() is True

    def test_returns_false_when_docker(self):
        """Should return False when in Docker mode."""
        with patch("app.core.platform.detect_deployment_mode", return_value=DeploymentMode.DOCKER):
            assert is_local_mode() is False


class TestIsDockerMode:
    """Test is_docker_mode helper."""

    def test_returns_true_when_docker(self):
        """Should return True when in Docker mode."""
        with patch("app.core.platform.detect_deployment_mode", return_value=DeploymentMode.DOCKER):
            assert is_docker_mode() is True

    def test_returns_false_when_local(self):
        """Should return False when in local mode."""
        with patch("app.core.platform.detect_deployment_mode", return_value=DeploymentMode.LOCAL):
            assert is_docker_mode() is False


class TestGetPlatformInfo:
    """Test get_platform_info diagnostic function."""

    def test_returns_dict_with_all_keys(self):
        """Should return dictionary with all expected keys."""
        with patch.dict(os.environ, {"LOCAL_MODE": "true"}, clear=False):
            with patch.object(Path, "home", return_value=Path("/home/testuser")):
                with patch.object(Path, "cwd", return_value=Path("/current/dir")):
                    with patch("shutil.which", return_value=None):
                        with patch.object(Path, "exists", return_value=False):
                            result = get_platform_info()

                            expected_keys = {
                                "platform",
                                "python_version",
                                "deployment_mode",
                                "app_data_dir",
                                "workspace_dir",
                                "claude_credentials_dir",
                                "claude_executable",
                                "gh_executable",
                                "home_dir",
                                "cwd",
                            }
                            assert set(result.keys()) == expected_keys

    def test_platform_matches_sys_platform(self):
        """Platform should match sys.platform."""
        with patch.dict(os.environ, {"LOCAL_MODE": "true"}, clear=False):
            with patch("shutil.which", return_value=None):
                with patch.object(Path, "exists", return_value=False):
                    result = get_platform_info()
                    assert result["platform"] == sys.platform

    def test_python_version_matches_sys_version(self):
        """Python version should match sys.version."""
        with patch.dict(os.environ, {"LOCAL_MODE": "true"}, clear=False):
            with patch("shutil.which", return_value=None):
                with patch.object(Path, "exists", return_value=False):
                    result = get_platform_info()
                    assert result["python_version"] == sys.version

    def test_deployment_mode_is_string(self):
        """Deployment mode should be the string value."""
        with patch.dict(os.environ, {"LOCAL_MODE": "true"}, clear=False):
            with patch("shutil.which", return_value=None):
                with patch.object(Path, "exists", return_value=False):
                    result = get_platform_info()
                    assert result["deployment_mode"] == "local"

    def test_paths_are_strings(self):
        """Path values should be converted to strings."""
        with patch.dict(os.environ, {"LOCAL_MODE": "true"}, clear=False):
            with patch("shutil.which", return_value=None):
                with patch.object(Path, "exists", return_value=False):
                    result = get_platform_info()
                    assert isinstance(result["app_data_dir"], str)
                    assert isinstance(result["workspace_dir"], str)
                    assert isinstance(result["claude_credentials_dir"], str)
                    assert isinstance(result["home_dir"], str)
                    assert isinstance(result["cwd"], str)

    def test_executables_can_be_none(self):
        """Executables should be None when not found."""
        with patch.dict(os.environ, {"LOCAL_MODE": "true"}, clear=False):
            with patch("shutil.which", return_value=None):
                with patch.object(Path, "exists", return_value=False):
                    result = get_platform_info()
                    assert result["claude_executable"] is None
                    assert result["gh_executable"] is None

    def test_executables_with_found_paths(self):
        """Executables should have paths when found."""
        with patch.dict(os.environ, {"LOCAL_MODE": "true"}, clear=False):
            with patch("shutil.which") as mock_which:
                def which_side_effect(name):
                    if name == "claude":
                        return "/usr/bin/claude"
                    if name == "gh":
                        return "/usr/bin/gh"
                    return None
                mock_which.side_effect = which_side_effect
                with patch.object(Path, "exists", return_value=False):
                    result = get_platform_info()
                    assert result["claude_executable"] == "/usr/bin/claude"
                    assert result["gh_executable"] == "/usr/bin/gh"


class TestDetectDeploymentModePartialMounts:
    """Test deployment mode when only some Docker volume mounts exist."""

    @pytest.fixture(autouse=True)
    def clear_docker_env(self):
        """Ensure no Docker indicators in environment."""
        env_copy = os.environ.copy()
        for key in ["LOCAL_MODE", "USER", "HOME"]:
            if key in os.environ:
                del os.environ[key]
        os.environ["USER"] = "testuser"
        os.environ["HOME"] = "/home/testuser"
        yield
        os.environ.clear()
        os.environ.update(env_copy)

    def test_only_data_exists(self):
        """Only /data existing should NOT trigger Docker mode."""
        def mock_exists(path_self):
            path_str = str(path_self)
            if path_str == "/.dockerenv":
                return False
            if path_str == "/data":
                return True
            return False

        with patch.object(Path, "exists", mock_exists):
            assert detect_deployment_mode() == DeploymentMode.LOCAL

    def test_only_workspace_exists(self):
        """Only /workspace existing should NOT trigger Docker mode."""
        def mock_exists(path_self):
            path_str = str(path_self)
            if path_str == "/.dockerenv":
                return False
            if path_str == "/workspace":
                return True
            return False

        with patch.object(Path, "exists", mock_exists):
            assert detect_deployment_mode() == DeploymentMode.LOCAL

    def test_data_and_workspace_but_no_app(self):
        """Only /data and /workspace existing should NOT trigger Docker mode."""
        def mock_exists(path_self):
            path_str = str(path_self)
            if path_str == "/.dockerenv":
                return False
            if path_str in ("/data", "/workspace"):
                return True
            return False

        with patch.object(Path, "exists", mock_exists):
            assert detect_deployment_mode() == DeploymentMode.LOCAL


class TestWindowsEmptyEnvVars:
    """Test Windows behavior with empty environment variables."""

    def test_empty_appdata(self):
        """Windows should handle empty APPDATA."""
        with patch("shutil.which", return_value=None):
            with patch("app.core.platform.sys") as mock_sys:
                mock_sys.platform = "win32"
                with patch.dict(os.environ, {"APPDATA": "", "LOCALAPPDATA": "", "LOCAL_MODE": "true"}, clear=False):
                    with patch.object(Path, "exists", return_value=False):
                        # Should not crash
                        result = get_claude_executable()
                        assert result is None


class TestMacOSExecutablePaths:
    """Test macOS-specific executable detection paths."""

    @pytest.mark.skipif(sys.platform == "win32", reason="Unix path tests require non-Windows platform")
    def test_darwin_platform_uses_unix_paths(self):
        """macOS should use Unix-style paths for executables."""
        def mock_exists(path_self):
            return str(path_self) == "/usr/local/bin/claude"

        with patch("shutil.which", return_value=None):
            with patch.object(Path, "home", return_value=Path("/Users/macuser")):
                with patch.object(Path, "exists", mock_exists):
                    result = get_claude_executable()
                    assert result == "/usr/local/bin/claude"

    @pytest.mark.skipif(sys.platform == "win32", reason="Unix path tests require non-Windows platform")
    def test_darwin_gh_paths(self):
        """macOS should check Unix paths for gh."""
        def mock_exists(path_self):
            return str(path_self) == "/usr/local/bin/gh"

        with patch("shutil.which", return_value=None):
            with patch.object(Path, "home", return_value=Path("/Users/macuser")):
                with patch.object(Path, "exists", mock_exists):
                    result = get_gh_executable()
                    assert result == "/usr/local/bin/gh"


class TestDeploymentModeEdgeCases:
    """Test edge cases in deployment mode detection."""

    def test_empty_local_mode_env_not_matched(self):
        """Empty LOCAL_MODE string should not match true/false."""
        with patch.dict(os.environ, {"LOCAL_MODE": ""}, clear=False):
            with patch.object(Path, "exists", return_value=False):
                with patch.dict(os.environ, {"USER": "testuser", "HOME": "/home/testuser"}, clear=False):
                    # Should fall through to LOCAL (no Docker indicators)
                    result = detect_deployment_mode()
                    assert result == DeploymentMode.LOCAL

    def test_random_local_mode_value_not_matched(self):
        """Random LOCAL_MODE value should not match true/false/1/0."""
        with patch.dict(os.environ, {"LOCAL_MODE": "random"}, clear=False):
            with patch.object(Path, "exists", return_value=False):
                with patch.dict(os.environ, {"USER": "testuser", "HOME": "/home/testuser"}, clear=False):
                    # Should fall through to LOCAL (no Docker indicators)
                    result = detect_deployment_mode()
                    assert result == DeploymentMode.LOCAL
