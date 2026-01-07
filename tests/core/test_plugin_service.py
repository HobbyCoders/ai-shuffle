"""
Unit tests for plugin service module.

Tests cover:
- Plugin scopes (USER, LOCAL, PROJECT)
- Marketplace management (add, remove, sync)
- Plugin installation and uninstallation
- Plugin enable/disable
- Available plugins discovery
- Installed plugins listing
- Plugin details retrieval
- File-based agents parsing
- JSON file I/O
- Error handling and edge cases
- Global service cache management
"""

import json
import pytest
import shutil
import subprocess
from pathlib import Path
from datetime import datetime
from unittest.mock import MagicMock, patch, mock_open

from app.core.plugin_service import (
    PluginScope,
    MarketplaceInfo,
    PluginInstallation,
    PluginInfo,
    PluginDetails,
    AvailablePlugin,
    PluginService,
    get_plugin_service,
    clear_plugin_service_cache,
    _get_claude_dir,
)


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def temp_claude_dir(tmp_path):
    """Create a temporary Claude directory structure."""
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir(parents=True)

    plugins_dir = claude_dir / "plugins"
    plugins_dir.mkdir()

    cache_dir = plugins_dir / "cache"
    cache_dir.mkdir()

    marketplaces_dir = plugins_dir / "marketplaces"
    marketplaces_dir.mkdir()

    return claude_dir


@pytest.fixture
def temp_project_dir(tmp_path):
    """Create a temporary project directory with plugin structures."""
    project_dir = tmp_path / "test_project"
    project_dir.mkdir()

    # Create local plugins directory
    local_plugins = project_dir / ".claude" / "plugins"
    local_plugins.mkdir(parents=True)

    # Create project plugins directory
    project_plugins = project_dir / "plugins"
    project_plugins.mkdir()

    return project_dir


@pytest.fixture
def mock_platform():
    """Mock platform detection to return local mode."""
    with patch("app.core.plugin_service.is_docker_mode", return_value=False):
        with patch("app.core.plugin_service.get_claude_credentials_dir") as mock_creds:
            yield mock_creds


@pytest.fixture
def plugin_service(temp_claude_dir, mock_platform):
    """Create a plugin service with mocked paths."""
    mock_platform.return_value = temp_claude_dir
    clear_plugin_service_cache()
    service = PluginService()
    return service


@pytest.fixture
def plugin_service_with_project(temp_claude_dir, temp_project_dir, mock_platform):
    """Create a plugin service with project directory."""
    mock_platform.return_value = temp_claude_dir
    clear_plugin_service_cache()
    service = PluginService(project_dir=temp_project_dir)
    return service


@pytest.fixture
def sample_marketplace_data():
    """Sample marketplace data for testing."""
    return {
        "test-marketplace": {
            "source": {
                "source": "git",
                "url": "https://github.com/test/plugins.git"
            },
            "installLocation": "/path/to/marketplace",
            "lastUpdated": "2024-01-15T10:30:00Z"
        }
    }


@pytest.fixture
def sample_installed_plugins_v2():
    """Sample installed plugins data (version 2 format)."""
    return {
        "version": 2,
        "plugins": {
            "test-plugin@test-marketplace": [{
                "scope": "user",
                "installPath": "/path/to/plugin",
                "version": "abc123",
                "installedAt": "2024-01-15T10:30:00Z",
                "lastUpdated": "2024-01-15T10:30:00Z",
                "isLocal": True
            }]
        }
    }


@pytest.fixture
def sample_installed_plugins_v1():
    """Sample installed plugins data (version 1 format)."""
    return {
        "version": 1,
        "plugins": {
            "test-plugin@test-marketplace": {
                "scope": "user",
                "installPath": "/path/to/plugin",
                "version": "abc123",
                "installedAt": "2024-01-15T10:30:00Z",
                "lastUpdated": "2024-01-15T10:30:00Z",
                "isLocal": True
            }
        }
    }


@pytest.fixture
def sample_settings():
    """Sample settings data."""
    return {
        "enabledPlugins": {
            "test-plugin@test-marketplace": True,
            "disabled-plugin@test-marketplace": False
        }
    }


# =============================================================================
# Test Data Classes
# =============================================================================

class TestMarketplaceInfo:
    """Test MarketplaceInfo dataclass."""

    def test_to_dict_without_last_updated(self):
        """to_dict should handle None last_updated."""
        info = MarketplaceInfo(
            id="test-mp",
            source="git",
            url="https://github.com/test/plugins.git",
            install_location="/path/to/mp",
            last_updated=None
        )

        result = info.to_dict()

        assert result["id"] == "test-mp"
        assert result["source"] == "git"
        assert result["url"] == "https://github.com/test/plugins.git"
        assert result["install_location"] == "/path/to/mp"
        assert result["last_updated"] is None

    def test_to_dict_with_last_updated(self):
        """to_dict should format datetime correctly."""
        dt = datetime(2024, 1, 15, 10, 30, 0)
        info = MarketplaceInfo(
            id="test-mp",
            source="git",
            url="https://github.com/test/plugins.git",
            install_location="/path/to/mp",
            last_updated=dt
        )

        result = info.to_dict()

        assert result["last_updated"] == "2024-01-15T10:30:00"


class TestPluginInfo:
    """Test PluginInfo dataclass."""

    def test_to_dict_without_installed_at(self):
        """to_dict should handle None installed_at."""
        info = PluginInfo(
            id="test-plugin@test-mp",
            name="test-plugin",
            marketplace="test-mp",
            description="A test plugin",
            version="1.0.0",
            scope="user",
            install_path="/path/to/plugin",
            installed_at=None,
            enabled=True,
            has_commands=True,
            has_agents=False,
            has_skills=False,
            has_hooks=False
        )

        result = info.to_dict()

        assert result["id"] == "test-plugin@test-mp"
        assert result["name"] == "test-plugin"
        assert result["installed_at"] is None
        assert result["enabled"] is True
        assert result["has_commands"] is True

    def test_to_dict_with_installed_at(self):
        """to_dict should format installed_at correctly."""
        dt = datetime(2024, 1, 15, 10, 30, 0)
        info = PluginInfo(
            id="test-plugin@test-mp",
            name="test-plugin",
            marketplace="test-mp",
            description="A test plugin",
            version="1.0.0",
            scope="user",
            install_path="/path/to/plugin",
            installed_at=dt,
            enabled=False
        )

        result = info.to_dict()

        assert result["installed_at"] == "2024-01-15T10:30:00"


class TestPluginDetailsDataclass:
    """Test PluginDetails dataclass."""

    def test_to_dict_includes_extended_fields(self):
        """to_dict should include commands, agents, skills, and readme."""
        details = PluginDetails(
            id="test-plugin@test-mp",
            name="test-plugin",
            marketplace="test-mp",
            description="A test plugin",
            version="1.0.0",
            scope="user",
            install_path="/path/to/plugin",
            commands=["cmd1", "cmd2"],
            agents=["agent1"],
            skills=["skill1"],
            readme="# Test Plugin\n\nThis is a test."
        )

        result = details.to_dict()

        assert result["commands"] == ["cmd1", "cmd2"]
        assert result["agents"] == ["agent1"]
        assert result["skills"] == ["skill1"]
        assert result["readme"] == "# Test Plugin\n\nThis is a test."

    def test_to_dict_includes_parent_fields(self):
        """to_dict should include fields from parent PluginInfo class."""
        details = PluginDetails(
            id="test-plugin@test-mp",
            name="test-plugin",
            marketplace="test-mp",
            description="A test plugin",
            version="1.0.0",
            scope="user",
            install_path="/path/to/plugin",
            enabled=True,
            has_commands=True,
            commands=["cmd1"],
            agents=[],
            skills=[],
            readme=None
        )

        result = details.to_dict()

        # Check parent class fields are included
        assert result["id"] == "test-plugin@test-mp"
        assert result["name"] == "test-plugin"
        assert result["marketplace"] == "test-mp"
        assert result["description"] == "A test plugin"
        assert result["version"] == "1.0.0"
        assert result["scope"] == "user"
        assert result["enabled"] is True
        assert result["has_commands"] is True
        # Check child class fields
        assert result["commands"] == ["cmd1"]
        assert result["readme"] is None


class TestAvailablePlugin:
    """Test AvailablePlugin dataclass."""

    def test_to_dict_returns_all_fields(self):
        """to_dict should return all fields."""
        plugin = AvailablePlugin(
            id="test-plugin@test-mp",
            name="test-plugin",
            marketplace="test-mp",
            marketplace_path="/path/to/plugin",
            description="A test plugin",
            has_commands=True,
            has_agents=True,
            has_skills=False,
            has_hooks=True,
            installed=True,
            enabled=True
        )

        result = plugin.to_dict()

        assert result["id"] == "test-plugin@test-mp"
        assert result["has_commands"] is True
        assert result["has_agents"] is True
        assert result["has_skills"] is False
        assert result["has_hooks"] is True
        assert result["installed"] is True
        assert result["enabled"] is True


# =============================================================================
# Test _get_claude_dir
# =============================================================================

class TestGetClaudeDir:
    """Test _get_claude_dir function."""

    def test_docker_mode_returns_docker_path(self):
        """Docker mode should return /home/appuser/.claude."""
        with patch("app.core.plugin_service.is_docker_mode", return_value=True):
            result = _get_claude_dir()
            assert result == Path("/home/appuser/.claude")

    def test_local_mode_returns_credentials_dir(self):
        """Local mode should return Claude credentials directory."""
        expected_path = Path("/home/user/.claude")
        with patch("app.core.plugin_service.is_docker_mode", return_value=False):
            with patch("app.core.plugin_service.get_claude_credentials_dir", return_value=expected_path):
                result = _get_claude_dir()
                assert result == expected_path


# =============================================================================
# Test PluginService Initialization
# =============================================================================

class TestPluginServiceInit:
    """Test PluginService initialization."""

    def test_init_without_project_dir(self, temp_claude_dir, mock_platform):
        """Init without project_dir should only set user scope paths."""
        mock_platform.return_value = temp_claude_dir

        service = PluginService()

        assert service.CLAUDE_DIR == temp_claude_dir
        assert service.PLUGINS_DIR == temp_claude_dir / "plugins"
        assert service.project_dir is None
        assert service.LOCAL_PLUGINS_DIR is None
        assert service.PROJECT_PLUGINS_DIR is None

    def test_init_with_project_dir(self, temp_claude_dir, temp_project_dir, mock_platform):
        """Init with project_dir should set all scope paths."""
        mock_platform.return_value = temp_claude_dir

        service = PluginService(project_dir=temp_project_dir)

        assert service.project_dir == temp_project_dir
        assert service.LOCAL_PLUGINS_DIR == temp_project_dir / ".claude" / "plugins"
        assert service.PROJECT_PLUGINS_DIR == temp_project_dir / "plugins"

    def test_ensure_directories_creates_required_dirs(self, temp_claude_dir, mock_platform):
        """_ensure_directories should create required directories."""
        mock_platform.return_value = temp_claude_dir

        # Remove directories to test creation
        shutil.rmtree(temp_claude_dir / "plugins")

        service = PluginService()

        assert service.PLUGINS_DIR.exists()
        assert service.CACHE_DIR.exists()
        assert service.MARKETPLACES_DIR.exists()


# =============================================================================
# Test Plugin Scope Methods
# =============================================================================

class TestPluginScopeManagement:
    """Test plugin scope management methods."""

    def test_get_plugins_dir_for_scope_user(self, plugin_service):
        """USER scope should return plugins directory."""
        result = plugin_service.get_plugins_dir_for_scope(PluginScope.USER)
        assert result == plugin_service.PLUGINS_DIR

    def test_get_plugins_dir_for_scope_local_without_project(self, plugin_service):
        """LOCAL scope without project should return None."""
        result = plugin_service.get_plugins_dir_for_scope(PluginScope.LOCAL)
        assert result is None

    def test_get_plugins_dir_for_scope_local_with_project(self, plugin_service_with_project, temp_project_dir):
        """LOCAL scope with project should return local plugins dir."""
        result = plugin_service_with_project.get_plugins_dir_for_scope(PluginScope.LOCAL)
        assert result == temp_project_dir / ".claude" / "plugins"

    def test_get_plugins_dir_for_scope_project_without_project(self, plugin_service):
        """PROJECT scope without project should return None."""
        result = plugin_service.get_plugins_dir_for_scope(PluginScope.PROJECT)
        assert result is None

    def test_get_plugins_dir_for_scope_project_with_project(self, plugin_service_with_project, temp_project_dir):
        """PROJECT scope with project should return project plugins dir."""
        result = plugin_service_with_project.get_plugins_dir_for_scope(PluginScope.PROJECT)
        assert result == temp_project_dir / "plugins"

    def test_ensure_scope_directory_creates_dir(self, plugin_service_with_project, temp_project_dir):
        """ensure_scope_directory should create the directory."""
        # Remove the local plugins dir
        local_dir = temp_project_dir / ".claude" / "plugins"
        shutil.rmtree(local_dir)

        result = plugin_service_with_project.ensure_scope_directory(PluginScope.LOCAL)

        assert result == local_dir
        assert local_dir.exists()

    def test_ensure_scope_directory_returns_none_for_unavailable_scope(self, plugin_service):
        """ensure_scope_directory should return None for unavailable scope."""
        result = plugin_service.ensure_scope_directory(PluginScope.LOCAL)
        assert result is None


# =============================================================================
# Test JSON File I/O
# =============================================================================

class TestJsonFileIO:
    """Test JSON file reading and writing."""

    def test_read_json_file_existing_file(self, plugin_service, temp_claude_dir):
        """Should read existing JSON file correctly."""
        test_file = temp_claude_dir / "test.json"
        test_data = {"key": "value", "number": 42}
        test_file.write_text(json.dumps(test_data))

        result = plugin_service._read_json_file(test_file)

        assert result == test_data

    def test_read_json_file_nonexistent_returns_default(self, plugin_service, temp_claude_dir):
        """Should return default for nonexistent file."""
        test_file = temp_claude_dir / "nonexistent.json"

        result = plugin_service._read_json_file(test_file, {"default": True})

        assert result == {"default": True}

    def test_read_json_file_nonexistent_returns_empty_dict_by_default(self, plugin_service, temp_claude_dir):
        """Should return empty dict for nonexistent file when no default given."""
        test_file = temp_claude_dir / "nonexistent.json"

        result = plugin_service._read_json_file(test_file)

        assert result == {}

    def test_read_json_file_invalid_json_returns_default(self, plugin_service, temp_claude_dir):
        """Should return default for invalid JSON."""
        test_file = temp_claude_dir / "invalid.json"
        test_file.write_text("not valid json{")

        result = plugin_service._read_json_file(test_file, {"fallback": True})

        assert result == {"fallback": True}

    def test_write_json_file_success(self, plugin_service, temp_claude_dir):
        """Should write JSON file correctly."""
        test_file = temp_claude_dir / "output.json"
        test_data = {"written": True, "items": [1, 2, 3]}

        result = plugin_service._write_json_file(test_file, test_data)

        assert result is True
        assert test_file.exists()
        assert json.loads(test_file.read_text()) == test_data

    def test_write_json_file_atomic(self, plugin_service, temp_claude_dir):
        """Write should be atomic (use temp file then replace)."""
        test_file = temp_claude_dir / "atomic.json"
        test_data = {"atomic": True}

        # This should work without partial writes
        plugin_service._write_json_file(test_file, test_data)

        # Temp file should not exist after write
        temp_file = test_file.with_suffix('.tmp')
        assert not temp_file.exists()
        assert test_file.exists()

    def test_write_json_file_io_error(self, plugin_service):
        """Should handle I/O errors gracefully."""
        # Try to write to a directory that doesn't exist
        test_file = Path("/nonexistent/dir/file.json")

        result = plugin_service._write_json_file(test_file, {"data": True})

        assert result is False


# =============================================================================
# Test Marketplace Management
# =============================================================================

class TestMarketplaceManagement:
    """Test marketplace management methods."""

    def test_get_marketplaces_empty(self, plugin_service):
        """Should return empty list when no marketplaces."""
        result = plugin_service.get_marketplaces()
        assert result == []

    def test_get_marketplaces_with_data(self, plugin_service, sample_marketplace_data):
        """Should return marketplace info from file."""
        plugin_service._write_json_file(plugin_service.MARKETPLACES_FILE, sample_marketplace_data)

        result = plugin_service.get_marketplaces()

        assert len(result) == 1
        assert result[0].id == "test-marketplace"
        assert result[0].source == "git"
        assert result[0].url == "https://github.com/test/plugins.git"
        assert result[0].last_updated is not None

    def test_get_marketplaces_with_invalid_date(self, plugin_service):
        """Should handle invalid date gracefully."""
        data = {
            "test-mp": {
                "source": {"source": "git", "url": "https://example.com/repo.git"},
                "installLocation": "/path",
                "lastUpdated": "not-a-date"
            }
        }
        plugin_service._write_json_file(plugin_service.MARKETPLACES_FILE, data)

        result = plugin_service.get_marketplaces()

        assert len(result) == 1
        assert result[0].last_updated is None

    def test_add_marketplace_success(self, plugin_service):
        """Should add marketplace successfully."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)

            result = plugin_service.add_marketplace(
                "https://github.com/test/plugins.git",
                "test-plugins"
            )

            assert result.id == "test-plugins"
            assert result.source == "git"
            assert result.url == "https://github.com/test/plugins.git"
            mock_run.assert_called_once()

    def test_add_marketplace_derives_name_from_url(self, plugin_service):
        """Should derive marketplace name from URL."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)

            result = plugin_service.add_marketplace(
                "https://github.com/org/my-plugins.git"
            )

            assert result.id == "my-plugins"

    def test_add_marketplace_git_clone_failure(self, plugin_service):
        """Should raise error on git clone failure."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(
                1, "git clone", stderr=b"Clone failed"
            )

            with pytest.raises(RuntimeError, match="Failed to clone marketplace"):
                plugin_service.add_marketplace("https://github.com/test/plugins.git")

    def test_add_marketplace_timeout(self, plugin_service):
        """Should raise error on timeout."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired("git clone", 120)

            with pytest.raises(RuntimeError, match="timed out"):
                plugin_service.add_marketplace("https://github.com/test/plugins.git")

    def test_add_marketplace_existing_directory(self, plugin_service):
        """Should not clone if directory exists."""
        mp_dir = plugin_service.MARKETPLACES_DIR / "existing"
        mp_dir.mkdir()

        with patch("subprocess.run") as mock_run:
            result = plugin_service.add_marketplace(
                "https://github.com/test/existing.git",
                "existing"
            )

            assert result.id == "existing"
            mock_run.assert_not_called()

    def test_remove_marketplace_success(self, plugin_service, sample_marketplace_data):
        """Should remove marketplace successfully."""
        # Create marketplace directory
        mp_dir = plugin_service.MARKETPLACES_DIR / "test-marketplace"
        mp_dir.mkdir()
        sample_marketplace_data["test-marketplace"]["installLocation"] = str(mp_dir)

        plugin_service._write_json_file(plugin_service.MARKETPLACES_FILE, sample_marketplace_data)
        plugin_service._write_json_file(
            plugin_service.INSTALLED_FILE,
            {"version": 2, "plugins": {"test-plugin@test-marketplace": [{}]}}
        )
        plugin_service._write_json_file(
            plugin_service.SETTINGS_FILE,
            {"enabledPlugins": {"test-plugin@test-marketplace": True}}
        )

        result = plugin_service.remove_marketplace("test-marketplace")

        assert result is True
        assert not mp_dir.exists()

        # Check marketplace removed from file
        mp_data = plugin_service._read_json_file(plugin_service.MARKETPLACES_FILE)
        assert "test-marketplace" not in mp_data

        # Check plugins removed
        installed = plugin_service._read_json_file(plugin_service.INSTALLED_FILE)
        assert "test-plugin@test-marketplace" not in installed.get("plugins", {})

    def test_remove_marketplace_not_found(self, plugin_service):
        """Should raise error for nonexistent marketplace."""
        with pytest.raises(ValueError, match="Marketplace not found"):
            plugin_service.remove_marketplace("nonexistent")

    def test_sync_marketplace_success(self, plugin_service, sample_marketplace_data):
        """Should sync marketplace successfully."""
        mp_dir = plugin_service.MARKETPLACES_DIR / "test-marketplace"
        mp_dir.mkdir()
        sample_marketplace_data["test-marketplace"]["installLocation"] = str(mp_dir)
        plugin_service._write_json_file(plugin_service.MARKETPLACES_FILE, sample_marketplace_data)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)

            result = plugin_service.sync_marketplace("test-marketplace")

            assert result is True
            mock_run.assert_called_once()

    def test_sync_marketplace_not_found(self, plugin_service):
        """Should raise error for nonexistent marketplace."""
        with pytest.raises(ValueError, match="Marketplace not found"):
            plugin_service.sync_marketplace("nonexistent")

    def test_sync_marketplace_directory_not_found(self, plugin_service, sample_marketplace_data):
        """Should raise error if directory doesn't exist."""
        sample_marketplace_data["test-marketplace"]["installLocation"] = "/nonexistent/path"
        plugin_service._write_json_file(plugin_service.MARKETPLACES_FILE, sample_marketplace_data)

        with pytest.raises(RuntimeError, match="Marketplace directory not found"):
            plugin_service.sync_marketplace("test-marketplace")

    def test_sync_marketplace_git_pull_failure(self, plugin_service, sample_marketplace_data):
        """Should raise error on git pull failure."""
        mp_dir = plugin_service.MARKETPLACES_DIR / "test-marketplace"
        mp_dir.mkdir()
        sample_marketplace_data["test-marketplace"]["installLocation"] = str(mp_dir)
        plugin_service._write_json_file(plugin_service.MARKETPLACES_FILE, sample_marketplace_data)

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(
                1, "git pull", stderr=b"Pull failed"
            )

            with pytest.raises(RuntimeError, match="Failed to sync marketplace"):
                plugin_service.sync_marketplace("test-marketplace")

    def test_sync_marketplace_timeout(self, plugin_service, sample_marketplace_data):
        """Should raise error on timeout."""
        mp_dir = plugin_service.MARKETPLACES_DIR / "test-marketplace"
        mp_dir.mkdir()
        sample_marketplace_data["test-marketplace"]["installLocation"] = str(mp_dir)
        plugin_service._write_json_file(plugin_service.MARKETPLACES_FILE, sample_marketplace_data)

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired("git pull", 120)

            with pytest.raises(RuntimeError, match="timed out"):
                plugin_service.sync_marketplace("test-marketplace")


# =============================================================================
# Test Available Plugins
# =============================================================================

class TestAvailablePlugins:
    """Test available plugins discovery."""

    def test_get_available_plugins_empty(self, plugin_service):
        """Should return empty list when no marketplaces."""
        result = plugin_service.get_available_plugins()
        assert result == []

    def test_get_available_plugins_with_marketplace(self, plugin_service):
        """Should find plugins in marketplace."""
        # Create marketplace with plugins
        mp_dir = plugin_service.MARKETPLACES_DIR / "test-mp"
        plugins_dir = mp_dir / "plugins"
        plugin_dir = plugins_dir / "test-plugin"
        plugin_dir.mkdir(parents=True)

        # Create plugin components
        (plugin_dir / "commands").mkdir()
        (plugin_dir / "agents").mkdir()
        (plugin_dir / "README.md").write_text("# Test Plugin\n\nThis is a test plugin for testing.")

        # Create marketplace entry
        plugin_service._write_json_file(plugin_service.MARKETPLACES_FILE, {
            "test-mp": {
                "source": {"source": "git", "url": "https://example.com/repo.git"},
                "installLocation": str(mp_dir)
            }
        })

        result = plugin_service.get_available_plugins()

        assert len(result) == 1
        assert result[0].id == "test-plugin@test-mp"
        assert result[0].name == "test-plugin"
        assert result[0].has_commands is True
        assert result[0].has_agents is True
        assert result[0].has_skills is False
        assert result[0].description == "This is a test plugin for testing."

    def test_get_available_plugins_filter_by_marketplace(self, plugin_service):
        """Should filter plugins by marketplace."""
        # Create two marketplaces
        for mp_name in ["mp1", "mp2"]:
            mp_dir = plugin_service.MARKETPLACES_DIR / mp_name
            plugin_dir = mp_dir / "plugins" / f"plugin-{mp_name}"
            plugin_dir.mkdir(parents=True)

        plugin_service._write_json_file(plugin_service.MARKETPLACES_FILE, {
            "mp1": {"source": {"source": "git", "url": "url1"}, "installLocation": str(plugin_service.MARKETPLACES_DIR / "mp1")},
            "mp2": {"source": {"source": "git", "url": "url2"}, "installLocation": str(plugin_service.MARKETPLACES_DIR / "mp2")}
        })

        result = plugin_service.get_available_plugins(marketplace="mp1")

        assert len(result) == 1
        assert result[0].marketplace == "mp1"

    def test_get_available_plugins_marks_installed(self, plugin_service):
        """Should mark installed plugins correctly."""
        mp_dir = plugin_service.MARKETPLACES_DIR / "test-mp"
        plugin_dir = mp_dir / "plugins" / "test-plugin"
        plugin_dir.mkdir(parents=True)

        plugin_service._write_json_file(plugin_service.MARKETPLACES_FILE, {
            "test-mp": {"source": {"source": "git", "url": "url"}, "installLocation": str(mp_dir)}
        })

        plugin_service._write_json_file(plugin_service.INSTALLED_FILE, {
            "version": 2,
            "plugins": {"test-plugin@test-mp": [{"scope": "user"}]}
        })

        result = plugin_service.get_available_plugins()

        assert result[0].installed is True

    def test_get_available_plugins_marks_enabled(self, plugin_service):
        """Should mark enabled plugins correctly."""
        mp_dir = plugin_service.MARKETPLACES_DIR / "test-mp"
        plugin_dir = mp_dir / "plugins" / "test-plugin"
        plugin_dir.mkdir(parents=True)

        plugin_service._write_json_file(plugin_service.MARKETPLACES_FILE, {
            "test-mp": {"source": {"source": "git", "url": "url"}, "installLocation": str(mp_dir)}
        })

        plugin_service._write_json_file(plugin_service.SETTINGS_FILE, {
            "enabledPlugins": {"test-plugin@test-mp": True}
        })

        result = plugin_service.get_available_plugins()

        assert result[0].enabled is True

    def test_get_available_plugins_skips_files(self, plugin_service):
        """Should skip files (only process directories)."""
        mp_dir = plugin_service.MARKETPLACES_DIR / "test-mp"
        plugins_dir = mp_dir / "plugins"
        plugins_dir.mkdir(parents=True)

        # Create a file that looks like a plugin
        (plugins_dir / "not-a-plugin.md").write_text("readme")

        # Create an actual plugin directory
        (plugins_dir / "real-plugin").mkdir()

        plugin_service._write_json_file(plugin_service.MARKETPLACES_FILE, {
            "test-mp": {"source": {"source": "git", "url": "url"}, "installLocation": str(mp_dir)}
        })

        result = plugin_service.get_available_plugins()

        assert len(result) == 1
        assert result[0].name == "real-plugin"

    def test_get_available_plugins_handles_readme_read_error(self, plugin_service):
        """Should handle README read errors gracefully."""
        mp_dir = plugin_service.MARKETPLACES_DIR / "test-mp"
        plugin_dir = mp_dir / "plugins" / "test-plugin"
        plugin_dir.mkdir(parents=True)

        # Create unreadable README (simulate by using invalid encoding)
        readme = plugin_dir / "README.md"
        readme.write_bytes(b'\x80\x81\x82')  # Invalid UTF-8

        plugin_service._write_json_file(plugin_service.MARKETPLACES_FILE, {
            "test-mp": {"source": {"source": "git", "url": "url"}, "installLocation": str(mp_dir)}
        })

        result = plugin_service.get_available_plugins()

        assert len(result) == 1
        assert result[0].description == ""

    def test_get_available_plugins_detects_hooks_json(self, plugin_service):
        """Should detect hooks.json as hooks indicator."""
        mp_dir = plugin_service.MARKETPLACES_DIR / "test-mp"
        plugin_dir = mp_dir / "plugins" / "test-plugin"
        plugin_dir.mkdir(parents=True)

        (plugin_dir / "hooks.json").write_text("{}")

        plugin_service._write_json_file(plugin_service.MARKETPLACES_FILE, {
            "test-mp": {"source": {"source": "git", "url": "url"}, "installLocation": str(mp_dir)}
        })

        result = plugin_service.get_available_plugins()

        assert result[0].has_hooks is True

    def test_get_available_plugins_no_plugins_dir(self, plugin_service):
        """Should handle marketplace without plugins directory."""
        mp_dir = plugin_service.MARKETPLACES_DIR / "test-mp"
        mp_dir.mkdir(parents=True)
        # No plugins subdirectory

        plugin_service._write_json_file(plugin_service.MARKETPLACES_FILE, {
            "test-mp": {"source": {"source": "git", "url": "url"}, "installLocation": str(mp_dir)}
        })

        result = plugin_service.get_available_plugins()

        assert result == []


# =============================================================================
# Test Installed Plugins
# =============================================================================

class TestInstalledPlugins:
    """Test installed plugins retrieval."""

    def test_get_installed_plugins_empty(self, plugin_service):
        """Should return empty list when no plugins installed."""
        result = plugin_service.get_installed_plugins()
        assert result == []

    def test_get_installed_plugins_v2_format(self, plugin_service, tmp_path):
        """Should handle version 2 format (array of installations)."""
        # Create plugin directory
        plugin_path = tmp_path / "test-plugin"
        plugin_path.mkdir()
        (plugin_path / "commands").mkdir()
        (plugin_path / "README.md").write_text("# Test\n\nDescription here.")

        plugin_service._write_json_file(plugin_service.INSTALLED_FILE, {
            "version": 2,
            "plugins": {
                "test-plugin@test-mp": [{
                    "scope": "user",
                    "installPath": str(plugin_path),
                    "version": "1.0.0",
                    "installedAt": "2024-01-15T10:30:00Z"
                }]
            }
        })

        result = plugin_service.get_installed_plugins()

        assert len(result) == 1
        assert result[0].id == "test-plugin@test-mp"
        assert result[0].name == "test-plugin"
        assert result[0].marketplace == "test-mp"
        assert result[0].version == "1.0.0"
        assert result[0].has_commands is True
        assert result[0].description == "Description here."

    def test_get_installed_plugins_v1_format(self, plugin_service, tmp_path):
        """Should handle version 1 format (single object)."""
        plugin_path = tmp_path / "test-plugin"
        plugin_path.mkdir()

        plugin_service._write_json_file(plugin_service.INSTALLED_FILE, {
            "version": 1,
            "plugins": {
                "test-plugin@test-mp": {
                    "scope": "user",
                    "installPath": str(plugin_path),
                    "version": "1.0.0",
                    "installedAt": "2024-01-15T10:30:00Z"
                }
            }
        })

        result = plugin_service.get_installed_plugins()

        assert len(result) == 1
        assert result[0].id == "test-plugin@test-mp"

    def test_get_installed_plugins_handles_empty_installation_data(self, plugin_service):
        """Should skip plugins with empty installation data."""
        plugin_service._write_json_file(plugin_service.INSTALLED_FILE, {
            "version": 2,
            "plugins": {
                "empty-plugin@test-mp": None,
                "empty-array@test-mp": []
            }
        })

        result = plugin_service.get_installed_plugins()

        assert len(result) == 0

    def test_get_installed_plugins_parses_enabled_status(self, plugin_service, tmp_path):
        """Should correctly parse enabled status from settings."""
        plugin_path = tmp_path / "test-plugin"
        plugin_path.mkdir()

        plugin_service._write_json_file(plugin_service.INSTALLED_FILE, {
            "version": 2,
            "plugins": {"test-plugin@test-mp": [{"installPath": str(plugin_path), "version": "1.0"}]}
        })
        plugin_service._write_json_file(plugin_service.SETTINGS_FILE, {
            "enabledPlugins": {"test-plugin@test-mp": True}
        })

        result = plugin_service.get_installed_plugins()

        assert result[0].enabled is True

    def test_get_installed_plugins_handles_invalid_date(self, plugin_service, tmp_path):
        """Should handle invalid date gracefully."""
        plugin_path = tmp_path / "test-plugin"
        plugin_path.mkdir()

        plugin_service._write_json_file(plugin_service.INSTALLED_FILE, {
            "version": 2,
            "plugins": {
                "test-plugin@test-mp": [{
                    "installPath": str(plugin_path),
                    "version": "1.0",
                    "installedAt": "invalid-date"
                }]
            }
        })

        result = plugin_service.get_installed_plugins()

        assert result[0].installed_at is None

    def test_get_installed_plugins_handles_missing_marketplace(self, plugin_service, tmp_path):
        """Should handle plugin ID without marketplace."""
        plugin_path = tmp_path / "test-plugin"
        plugin_path.mkdir()

        plugin_service._write_json_file(plugin_service.INSTALLED_FILE, {
            "version": 2,
            "plugins": {
                "test-plugin": [{  # No @ in ID
                    "installPath": str(plugin_path),
                    "version": "1.0"
                }]
            }
        })

        result = plugin_service.get_installed_plugins()

        assert result[0].marketplace == "local"

    def test_get_installed_plugins_detects_all_component_types(self, plugin_service, tmp_path):
        """Should detect all component types."""
        plugin_path = tmp_path / "test-plugin"
        plugin_path.mkdir()
        (plugin_path / "commands").mkdir()
        (plugin_path / "agents").mkdir()
        (plugin_path / "skills").mkdir()
        (plugin_path / "hooks").mkdir()

        plugin_service._write_json_file(plugin_service.INSTALLED_FILE, {
            "version": 2,
            "plugins": {"test-plugin@test-mp": [{"installPath": str(plugin_path), "version": "1.0"}]}
        })

        result = plugin_service.get_installed_plugins()

        assert result[0].has_commands is True
        assert result[0].has_agents is True
        assert result[0].has_skills is True
        assert result[0].has_hooks is True


# =============================================================================
# Test Plugin Details Service Methods
# =============================================================================

class TestGetPluginDetails:
    """Test get_plugin_details service method."""

    def test_get_plugin_details_not_found(self, plugin_service):
        """Should return None for nonexistent plugin."""
        result = plugin_service.get_plugin_details("nonexistent@mp")
        assert result is None

    def test_get_plugin_details_success(self, plugin_service, tmp_path):
        """Should return detailed plugin info."""
        plugin_path = tmp_path / "test-plugin"
        plugin_path.mkdir()

        # Create commands
        commands_dir = plugin_path / "commands"
        commands_dir.mkdir()
        (commands_dir / "cmd1.md").write_text("# Command 1")
        (commands_dir / "cmd2.md").write_text("# Command 2")

        # Create agents
        agents_dir = plugin_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "agent1.md").write_text("# Agent 1")

        # Create skills
        skills_dir = plugin_path / "skills"
        skill1_dir = skills_dir / "skill1"
        skill1_dir.mkdir(parents=True)
        (skill1_dir / "SKILL.md").write_text("# Skill 1")

        # Create README
        (plugin_path / "README.md").write_text("# Test Plugin\n\nThis is the description.")

        plugin_service._write_json_file(plugin_service.INSTALLED_FILE, {
            "version": 2,
            "plugins": {
                "test-plugin@test-mp": [{
                    "installPath": str(plugin_path),
                    "version": "1.0.0",
                    "scope": "user",
                    "installedAt": "2024-01-15T10:30:00Z"
                }]
            }
        })

        result = plugin_service.get_plugin_details("test-plugin@test-mp")

        assert result is not None
        assert result.id == "test-plugin@test-mp"
        assert "cmd1" in result.commands
        assert "cmd2" in result.commands
        assert "agent1" in result.agents
        assert "skill1" in result.skills
        assert "Test Plugin" in result.readme
        assert result.description == "This is the description."

    def test_get_plugin_details_install_path_not_found(self, plugin_service):
        """Should return None if install path doesn't exist."""
        plugin_service._write_json_file(plugin_service.INSTALLED_FILE, {
            "version": 2,
            "plugins": {
                "test-plugin@test-mp": [{
                    "installPath": "/nonexistent/path",
                    "version": "1.0.0"
                }]
            }
        })

        result = plugin_service.get_plugin_details("test-plugin@test-mp")

        assert result is None

    def test_get_plugin_details_v1_format(self, plugin_service, tmp_path):
        """Should handle version 1 format."""
        plugin_path = tmp_path / "test-plugin"
        plugin_path.mkdir()

        plugin_service._write_json_file(plugin_service.INSTALLED_FILE, {
            "version": 1,
            "plugins": {
                "test-plugin@test-mp": {
                    "installPath": str(plugin_path),
                    "version": "1.0.0"
                }
            }
        })

        result = plugin_service.get_plugin_details("test-plugin@test-mp")

        assert result is not None

    def test_get_plugin_details_empty_array(self, plugin_service):
        """Should return None for empty array."""
        plugin_service._write_json_file(plugin_service.INSTALLED_FILE, {
            "version": 2,
            "plugins": {"test-plugin@test-mp": []}
        })

        result = plugin_service.get_plugin_details("test-plugin@test-mp")

        assert result is None

    def test_get_plugin_details_detects_hooks_json(self, plugin_service, tmp_path):
        """Should detect hooks.json file."""
        plugin_path = tmp_path / "test-plugin"
        plugin_path.mkdir()
        (plugin_path / "hooks.json").write_text("{}")

        plugin_service._write_json_file(plugin_service.INSTALLED_FILE, {
            "version": 2,
            "plugins": {"test-plugin@test-mp": [{"installPath": str(plugin_path), "version": "1.0"}]}
        })

        result = plugin_service.get_plugin_details("test-plugin@test-mp")

        assert result.has_hooks is True


# =============================================================================
# Test Plugin Installation
# =============================================================================

class TestPluginInstallation:
    """Test plugin installation."""

    def test_install_plugin_invalid_id_format(self, plugin_service):
        """Should raise error for invalid plugin ID format."""
        with pytest.raises(ValueError, match="Invalid plugin ID format"):
            plugin_service.install_plugin("invalid-no-at-sign")

    def test_install_plugin_marketplace_not_found(self, plugin_service):
        """Should raise error for nonexistent marketplace."""
        with pytest.raises(ValueError, match="Marketplace not found"):
            plugin_service.install_plugin("plugin@nonexistent")

    def test_install_plugin_plugin_not_found(self, plugin_service):
        """Should raise error for nonexistent plugin."""
        mp_dir = plugin_service.MARKETPLACES_DIR / "test-mp"
        mp_dir.mkdir()

        plugin_service._write_json_file(plugin_service.MARKETPLACES_FILE, {
            "test-mp": {"installLocation": str(mp_dir)}
        })

        with pytest.raises(ValueError, match="Plugin not found in marketplace"):
            plugin_service.install_plugin("nonexistent@test-mp")

    def test_install_plugin_success(self, plugin_service):
        """Should install plugin successfully."""
        # Create marketplace with plugin
        mp_dir = plugin_service.MARKETPLACES_DIR / "test-mp"
        plugin_source = mp_dir / "plugins" / "test-plugin"
        plugin_source.mkdir(parents=True)
        (plugin_source / "README.md").write_text("# Test Plugin")

        plugin_service._write_json_file(plugin_service.MARKETPLACES_FILE, {
            "test-mp": {"installLocation": str(mp_dir)}
        })

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="abc123")

            result = plugin_service.install_plugin("test-plugin@test-mp")

            assert result.name == "test-plugin"
            assert result.marketplace == "test-mp"
            assert result.enabled is True

    def test_install_plugin_uses_default_version_on_git_error(self, plugin_service):
        """Should use default version if git command fails."""
        mp_dir = plugin_service.MARKETPLACES_DIR / "test-mp"
        plugin_source = mp_dir / "plugins" / "test-plugin"
        plugin_source.mkdir(parents=True)

        plugin_service._write_json_file(plugin_service.MARKETPLACES_FILE, {
            "test-mp": {"installLocation": str(mp_dir)}
        })

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(1, "git", stderr=b"error")

            result = plugin_service.install_plugin("test-plugin@test-mp")

            # Should still install with default version
            assert result is not None

    def test_install_plugin_overwrites_existing_cache(self, plugin_service):
        """Should overwrite existing cache directory."""
        mp_dir = plugin_service.MARKETPLACES_DIR / "test-mp"
        plugin_source = mp_dir / "plugins" / "test-plugin"
        plugin_source.mkdir(parents=True)
        (plugin_source / "new_file.txt").write_text("new content")

        # Create existing cache
        cache_path = plugin_service.CACHE_DIR / "test-mp" / "test-plugin" / "1.0.0"
        cache_path.mkdir(parents=True)
        (cache_path / "old_file.txt").write_text("old content")

        plugin_service._write_json_file(plugin_service.MARKETPLACES_FILE, {
            "test-mp": {"installLocation": str(mp_dir)}
        })

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)  # Git fails, use default version

            plugin_service.install_plugin("test-plugin@test-mp")

            # Old file should be gone, new file should exist
            assert not (cache_path / "old_file.txt").exists()

    def test_install_plugins_batch_success(self, plugin_service):
        """Should install multiple plugins."""
        mp_dir = plugin_service.MARKETPLACES_DIR / "test-mp"
        for name in ["plugin1", "plugin2"]:
            plugin_source = mp_dir / "plugins" / name
            plugin_source.mkdir(parents=True)

        plugin_service._write_json_file(plugin_service.MARKETPLACES_FILE, {
            "test-mp": {"installLocation": str(mp_dir)}
        })

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)

            results = plugin_service.install_plugins_batch([
                "plugin1@test-mp",
                "plugin2@test-mp"
            ])

            assert len(results) == 2

    def test_install_plugins_batch_handles_errors(self, plugin_service):
        """Should continue installing even if some fail."""
        mp_dir = plugin_service.MARKETPLACES_DIR / "test-mp"
        plugin_source = mp_dir / "plugins" / "good-plugin"
        plugin_source.mkdir(parents=True)

        plugin_service._write_json_file(plugin_service.MARKETPLACES_FILE, {
            "test-mp": {"installLocation": str(mp_dir)}
        })

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)

            results = plugin_service.install_plugins_batch([
                "nonexistent@test-mp",  # Will fail
                "good-plugin@test-mp"   # Will succeed
            ])

            assert len(results) == 1


# =============================================================================
# Test Plugin Uninstallation
# =============================================================================

class TestPluginUninstallation:
    """Test plugin uninstallation."""

    def test_uninstall_plugin_not_found(self, plugin_service):
        """Should raise error for nonexistent plugin."""
        with pytest.raises(ValueError, match="Plugin not installed"):
            plugin_service.uninstall_plugin("nonexistent@mp")

    def test_uninstall_plugin_success(self, plugin_service, tmp_path):
        """Should uninstall plugin successfully."""
        plugin_path = tmp_path / "test-plugin"
        plugin_path.mkdir()
        (plugin_path / "file.txt").write_text("content")

        plugin_service._write_json_file(plugin_service.INSTALLED_FILE, {
            "version": 2,
            "plugins": {"test-plugin@test-mp": [{"installPath": str(plugin_path), "version": "1.0"}]}
        })
        plugin_service._write_json_file(plugin_service.SETTINGS_FILE, {
            "enabledPlugins": {"test-plugin@test-mp": True}
        })

        result = plugin_service.uninstall_plugin("test-plugin@test-mp")

        assert result is True
        assert not plugin_path.exists()

        # Check removed from installed
        installed = plugin_service._read_json_file(plugin_service.INSTALLED_FILE)
        assert "test-plugin@test-mp" not in installed.get("plugins", {})

        # Check removed from enabled
        settings = plugin_service._read_json_file(plugin_service.SETTINGS_FILE)
        assert "test-plugin@test-mp" not in settings.get("enabledPlugins", {})

    def test_uninstall_plugin_v1_format(self, plugin_service, tmp_path):
        """Should handle version 1 format."""
        plugin_path = tmp_path / "test-plugin"
        plugin_path.mkdir()

        plugin_service._write_json_file(plugin_service.INSTALLED_FILE, {
            "version": 1,
            "plugins": {"test-plugin@test-mp": {"installPath": str(plugin_path), "version": "1.0"}}
        })

        result = plugin_service.uninstall_plugin("test-plugin@test-mp")

        assert result is True

    def test_uninstall_plugin_handles_missing_path(self, plugin_service):
        """Should handle missing install path gracefully."""
        plugin_service._write_json_file(plugin_service.INSTALLED_FILE, {
            "version": 2,
            "plugins": {"test-plugin@test-mp": [{"installPath": "/nonexistent", "version": "1.0"}]}
        })

        result = plugin_service.uninstall_plugin("test-plugin@test-mp")

        assert result is True


# =============================================================================
# Test Plugin Enable/Disable
# =============================================================================

class TestPluginEnableDisable:
    """Test plugin enable/disable functionality."""

    def test_enable_plugin(self, plugin_service):
        """Should enable plugin in settings."""
        result = plugin_service.enable_plugin("test-plugin@test-mp")

        assert result is True
        settings = plugin_service._read_json_file(plugin_service.SETTINGS_FILE)
        assert settings["enabledPlugins"]["test-plugin@test-mp"] is True

    def test_disable_plugin(self, plugin_service):
        """Should disable plugin in settings."""
        result = plugin_service.disable_plugin("test-plugin@test-mp")

        assert result is True
        settings = plugin_service._read_json_file(plugin_service.SETTINGS_FILE)
        assert settings["enabledPlugins"]["test-plugin@test-mp"] is False

    def test_enable_plugin_creates_enabled_plugins_key(self, plugin_service):
        """Should create enabledPlugins key if missing."""
        plugin_service._write_json_file(plugin_service.SETTINGS_FILE, {})

        plugin_service.enable_plugin("test-plugin@test-mp")

        settings = plugin_service._read_json_file(plugin_service.SETTINGS_FILE)
        assert "enabledPlugins" in settings

    def test_get_enabled_plugins_empty(self, plugin_service):
        """Should return empty dict when no settings."""
        result = plugin_service.get_enabled_plugins()
        assert result == {}

    def test_get_enabled_plugins_with_data(self, plugin_service, sample_settings):
        """Should return enabled plugins from settings."""
        plugin_service._write_json_file(plugin_service.SETTINGS_FILE, sample_settings)

        result = plugin_service.get_enabled_plugins()

        assert result["test-plugin@test-marketplace"] is True
        assert result["disabled-plugin@test-marketplace"] is False


# =============================================================================
# Test File-Based Agents
# =============================================================================

class TestFileBasedAgents:
    """Test file-based agents retrieval."""

    def test_get_file_based_agents_no_plugins(self, plugin_service):
        """Should return empty list with no plugins."""
        result = plugin_service.get_file_based_agents()
        assert result == []

    def test_get_file_based_agents_disabled_plugin_skipped(self, plugin_service, tmp_path):
        """Should skip disabled plugins."""
        plugin_path = tmp_path / "test-plugin"
        agents_dir = plugin_path / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "agent1.md").write_text("# Agent")

        plugin_service._write_json_file(plugin_service.INSTALLED_FILE, {
            "version": 2,
            "plugins": {"test-plugin@test-mp": [{"installPath": str(plugin_path), "version": "1.0"}]}
        })
        plugin_service._write_json_file(plugin_service.SETTINGS_FILE, {
            "enabledPlugins": {"test-plugin@test-mp": False}
        })

        result = plugin_service.get_file_based_agents()

        assert result == []

    def test_get_file_based_agents_returns_agents(self, plugin_service, tmp_path):
        """Should return agents from enabled plugins."""
        plugin_path = tmp_path / "test-plugin"
        agents_dir = plugin_path / "agents"
        agents_dir.mkdir(parents=True)

        (agents_dir / "agent1.md").write_text("""---
description: Test agent
model: claude-3-opus
tools: tool1, tool2
---
You are a test agent.
""")

        plugin_service._write_json_file(plugin_service.INSTALLED_FILE, {
            "version": 2,
            "plugins": {"test-plugin@test-mp": [{"installPath": str(plugin_path), "version": "1.0"}]}
        })
        plugin_service._write_json_file(plugin_service.SETTINGS_FILE, {
            "enabledPlugins": {"test-plugin@test-mp": True}
        })

        result = plugin_service.get_file_based_agents()

        assert len(result) == 1
        assert result[0]["id"] == "test-plugin@test-mp:agent1"
        assert result[0]["name"] == "agent1"
        assert result[0]["description"] == "Test agent"
        assert result[0]["model"] == "claude-3-opus"
        assert result[0]["tools"] == ["tool1", "tool2"]
        assert "You are a test agent." in result[0]["prompt"]

    def test_get_file_based_agents_no_agents_dir(self, plugin_service, tmp_path):
        """Should handle plugins without agents directory."""
        plugin_path = tmp_path / "test-plugin"
        plugin_path.mkdir()

        plugin_service._write_json_file(plugin_service.INSTALLED_FILE, {
            "version": 2,
            "plugins": {"test-plugin@test-mp": [{"installPath": str(plugin_path), "version": "1.0"}]}
        })
        plugin_service._write_json_file(plugin_service.SETTINGS_FILE, {
            "enabledPlugins": {"test-plugin@test-mp": True}
        })

        result = plugin_service.get_file_based_agents()

        assert result == []


class TestParseAgentFrontmatter:
    """Test agent frontmatter parsing."""

    def test_parse_no_frontmatter(self, plugin_service):
        """Should return content as prompt when no frontmatter."""
        content = "This is just a prompt without frontmatter."

        result = plugin_service._parse_agent_frontmatter(content)

        assert result["prompt"] == content
        assert result["description"] == ""
        assert result["model"] is None
        assert result["tools"] is None

    def test_parse_incomplete_frontmatter(self, plugin_service):
        """Should handle incomplete frontmatter (no closing ---)."""
        content = """---
description: Test
This should be the prompt.
"""

        result = plugin_service._parse_agent_frontmatter(content)

        assert content in result["prompt"]

    def test_parse_valid_frontmatter(self, plugin_service):
        """Should parse valid frontmatter correctly."""
        content = """---
description: A helpful agent
model: claude-3-opus
tools: bash, read, write
---
You are a helpful assistant.
Do your best.
"""

        result = plugin_service._parse_agent_frontmatter(content)

        assert result["description"] == "A helpful agent"
        assert result["model"] == "claude-3-opus"
        assert result["tools"] == ["bash", "read", "write"]
        assert "You are a helpful assistant." in result["prompt"]
        assert "Do your best." in result["prompt"]

    def test_parse_empty_tools(self, plugin_service):
        """Should handle empty tools value."""
        content = """---
description: Test
tools:
---
Prompt here.
"""

        result = plugin_service._parse_agent_frontmatter(content)

        assert result["tools"] == []


# =============================================================================
# Test Global Service Cache
# =============================================================================

class TestGlobalServiceCache:
    """Test global plugin service cache management."""

    def test_get_plugin_service_caches_instance(self, temp_claude_dir, mock_platform):
        """Should cache and return same instance."""
        mock_platform.return_value = temp_claude_dir
        clear_plugin_service_cache()

        service1 = get_plugin_service()
        service2 = get_plugin_service()

        assert service1 is service2

    def test_get_plugin_service_different_project_dirs(self, temp_claude_dir, temp_project_dir, mock_platform, tmp_path):
        """Should cache different instances for different project dirs."""
        mock_platform.return_value = temp_claude_dir
        clear_plugin_service_cache()

        # Create another project directory
        project2 = tmp_path / "project2"
        project2.mkdir()

        service1 = get_plugin_service(temp_project_dir)
        service2 = get_plugin_service(project2)

        assert service1 is not service2

    def test_clear_plugin_service_cache(self, temp_claude_dir, mock_platform):
        """Should clear the cache."""
        mock_platform.return_value = temp_claude_dir

        service1 = get_plugin_service()
        clear_plugin_service_cache()
        service2 = get_plugin_service()

        assert service1 is not service2

    def test_get_plugin_service_with_none_project_dir(self, temp_claude_dir, mock_platform):
        """Should handle None project_dir."""
        mock_platform.return_value = temp_claude_dir
        clear_plugin_service_cache()

        service = get_plugin_service(None)

        assert service.project_dir is None


# =============================================================================
# Test Edge Cases and Error Handling
# =============================================================================

class TestEdgeCases:
    """Test edge cases and error handling."""

    def test_read_json_file_with_io_error(self, plugin_service, temp_claude_dir):
        """Should handle I/O errors when reading files."""
        test_file = temp_claude_dir / "test.json"
        test_file.write_text('{"key": "value"}')

        with patch("builtins.open", side_effect=IOError("Read error")):
            result = plugin_service._read_json_file(test_file, {"default": True})

            assert result == {"default": True}

    def test_marketplace_with_missing_source_info(self, plugin_service):
        """Should handle marketplace without source info."""
        plugin_service._write_json_file(plugin_service.MARKETPLACES_FILE, {
            "test-mp": {
                "installLocation": "/path"
            }
        })

        result = plugin_service.get_marketplaces()

        assert len(result) == 1
        assert result[0].source == "git"  # Default value
        assert result[0].url == ""  # Default value

    def test_get_available_plugins_extracts_first_non_header_line(self, plugin_service):
        """Should extract first non-header line as description."""
        mp_dir = plugin_service.MARKETPLACES_DIR / "test-mp"
        plugin_dir = mp_dir / "plugins" / "test-plugin"
        plugin_dir.mkdir(parents=True)

        readme_content = """# Plugin Title
## Another Header
### Yet another

This is the actual description line.
More content here.
"""
        (plugin_dir / "README.md").write_text(readme_content)

        plugin_service._write_json_file(plugin_service.MARKETPLACES_FILE, {
            "test-mp": {"source": {"source": "git", "url": "url"}, "installLocation": str(mp_dir)}
        })

        result = plugin_service.get_available_plugins()

        assert result[0].description == "This is the actual description line."

    def test_description_truncated_to_200_chars(self, plugin_service):
        """Should truncate description to 200 characters."""
        mp_dir = plugin_service.MARKETPLACES_DIR / "test-mp"
        plugin_dir = mp_dir / "plugins" / "test-plugin"
        plugin_dir.mkdir(parents=True)

        long_description = "A" * 300
        (plugin_dir / "README.md").write_text(f"# Title\n\n{long_description}")

        plugin_service._write_json_file(plugin_service.MARKETPLACES_FILE, {
            "test-mp": {"source": {"source": "git", "url": "url"}, "installLocation": str(mp_dir)}
        })

        result = plugin_service.get_available_plugins()

        assert len(result[0].description) == 200

    def test_install_plugin_with_scope_parameter(self, plugin_service):
        """Should respect scope parameter during installation."""
        mp_dir = plugin_service.MARKETPLACES_DIR / "test-mp"
        plugin_source = mp_dir / "plugins" / "test-plugin"
        plugin_source.mkdir(parents=True)

        plugin_service._write_json_file(plugin_service.MARKETPLACES_FILE, {
            "test-mp": {"installLocation": str(mp_dir)}
        })

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)

            result = plugin_service.install_plugin("test-plugin@test-mp", scope="project")

            assert result.scope == "project"

    def test_get_installed_plugins_handles_nonexistent_path(self, plugin_service):
        """Should handle nonexistent install path gracefully."""
        plugin_service._write_json_file(plugin_service.INSTALLED_FILE, {
            "version": 2,
            "plugins": {
                "test-plugin@test-mp": [{
                    "installPath": "/nonexistent/path",
                    "version": "1.0"
                }]
            }
        })

        result = plugin_service.get_installed_plugins()

        assert len(result) == 1
        assert result[0].has_commands is False
        assert result[0].has_agents is False

    def test_get_installed_plugins_readme_with_invalid_encoding(self, plugin_service, tmp_path):
        """Should handle README with invalid encoding in get_installed_plugins."""
        plugin_path = tmp_path / "test-plugin"
        plugin_path.mkdir()

        # Create README with invalid UTF-8
        (plugin_path / "README.md").write_bytes(b'\x80\x81\x82 invalid utf8')

        plugin_service._write_json_file(plugin_service.INSTALLED_FILE, {
            "version": 2,
            "plugins": {
                "test-plugin@test-mp": [{
                    "installPath": str(plugin_path),
                    "version": "1.0"
                }]
            }
        })

        result = plugin_service.get_installed_plugins()

        assert len(result) == 1
        assert result[0].description == ""  # Should fallback to empty

    def test_get_plugin_details_readme_with_invalid_encoding(self, plugin_service, tmp_path):
        """Should handle README with invalid encoding in get_plugin_details."""
        plugin_path = tmp_path / "test-plugin"
        plugin_path.mkdir()

        # Create README with invalid UTF-8
        (plugin_path / "README.md").write_bytes(b'\x80\x81\x82 invalid utf8')

        plugin_service._write_json_file(plugin_service.INSTALLED_FILE, {
            "version": 2,
            "plugins": {
                "test-plugin@test-mp": [{
                    "installPath": str(plugin_path),
                    "version": "1.0"
                }]
            }
        })

        result = plugin_service.get_plugin_details("test-plugin@test-mp")

        assert result is not None
        assert result.readme is None
        assert result.description == ""

    def test_get_plugin_details_with_invalid_date(self, plugin_service, tmp_path):
        """Should handle invalid date in get_plugin_details."""
        plugin_path = tmp_path / "test-plugin"
        plugin_path.mkdir()

        plugin_service._write_json_file(plugin_service.INSTALLED_FILE, {
            "version": 2,
            "plugins": {
                "test-plugin@test-mp": [{
                    "installPath": str(plugin_path),
                    "version": "1.0",
                    "installedAt": "not-a-valid-date"
                }]
            }
        })

        result = plugin_service.get_plugin_details("test-plugin@test-mp")

        assert result is not None
        assert result.installed_at is None

    def test_get_plugin_details_without_marketplace_in_id(self, plugin_service, tmp_path):
        """Should handle plugin ID without marketplace in get_plugin_details."""
        plugin_path = tmp_path / "test-plugin"
        plugin_path.mkdir()

        plugin_service._write_json_file(plugin_service.INSTALLED_FILE, {
            "version": 2,
            "plugins": {
                "local-plugin": [{  # No @ in ID
                    "installPath": str(plugin_path),
                    "version": "1.0"
                }]
            }
        })

        result = plugin_service.get_plugin_details("local-plugin")

        assert result is not None
        assert result.marketplace == "local"

    def test_install_plugin_with_missing_plugins_key(self, plugin_service):
        """Should handle installed file without 'plugins' key."""
        mp_dir = plugin_service.MARKETPLACES_DIR / "test-mp"
        plugin_source = mp_dir / "plugins" / "test-plugin"
        plugin_source.mkdir(parents=True)

        plugin_service._write_json_file(plugin_service.MARKETPLACES_FILE, {
            "test-mp": {"installLocation": str(mp_dir)}
        })

        # Write installed file without 'plugins' key
        plugin_service._write_json_file(plugin_service.INSTALLED_FILE, {
            "version": 2
        })

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)

            result = plugin_service.install_plugin("test-plugin@test-mp")

            assert result is not None

    def test_get_file_based_agents_handles_io_error(self, plugin_service, tmp_path):
        """Should handle I/O error when reading agent file."""
        plugin_path = tmp_path / "test-plugin"
        agents_dir = plugin_path / "agents"
        agents_dir.mkdir(parents=True)

        # Create an agent file
        agent_file = agents_dir / "agent1.md"
        agent_file.write_text("# Agent")

        plugin_service._write_json_file(plugin_service.INSTALLED_FILE, {
            "version": 2,
            "plugins": {"test-plugin@test-mp": [{"installPath": str(plugin_path), "version": "1.0"}]}
        })
        plugin_service._write_json_file(plugin_service.SETTINGS_FILE, {
            "enabledPlugins": {"test-plugin@test-mp": True}
        })

        # Patch file read to raise IOError
        original_read_text = Path.read_text
        def mock_read_text(self, *args, **kwargs):
            if "agent1.md" in str(self):
                raise IOError("Cannot read file")
            return original_read_text(self, *args, **kwargs)

        with patch.object(Path, 'read_text', mock_read_text):
            result = plugin_service.get_file_based_agents()

        # Should log error but continue, returning empty list (since agent read failed)
        assert result == []

    def test_get_plugins_dir_for_invalid_scope(self, plugin_service):
        """Should return None for invalid scope enum value."""
        # Create a mock scope that's not one of the expected values
        class FakeScope:
            pass

        fake_scope = FakeScope()
        # Manually test with an unexpected value by bypassing enum
        result = plugin_service.get_plugins_dir_for_scope(fake_scope)
        assert result is None


# =============================================================================
# Test Additional Coverage
# =============================================================================

class TestAdditionalCoverage:
    """Additional tests for complete coverage."""

    def test_install_plugin_timeout_on_git_version(self, plugin_service):
        """Should handle timeout when getting git version."""
        mp_dir = plugin_service.MARKETPLACES_DIR / "test-mp"
        plugin_source = mp_dir / "plugins" / "test-plugin"
        plugin_source.mkdir(parents=True)

        plugin_service._write_json_file(plugin_service.MARKETPLACES_FILE, {
            "test-mp": {"installLocation": str(mp_dir)}
        })

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired("git", 10)

            result = plugin_service.install_plugin("test-plugin@test-mp")

            # Should use default version and complete installation
            assert result is not None

    def test_get_installed_plugins_v2_dict_format_with_version_2(self, plugin_service, tmp_path):
        """Test v2 format where installation data is a dict (edge case)."""
        plugin_path = tmp_path / "test-plugin"
        plugin_path.mkdir()

        # Version 2 but with dict instead of array (unusual but possible)
        plugin_service._write_json_file(plugin_service.INSTALLED_FILE, {
            "version": 2,
            "plugins": {
                "test-plugin@test-mp": {
                    "scope": "user",
                    "installPath": str(plugin_path),
                    "version": "1.0"
                }
            }
        })

        result = plugin_service.get_installed_plugins()

        assert len(result) == 1
        assert result[0].id == "test-plugin@test-mp"

    def test_get_plugin_details_v2_dict_format(self, plugin_service, tmp_path):
        """Test get_plugin_details with v2 format where data is dict."""
        plugin_path = tmp_path / "test-plugin"
        plugin_path.mkdir()

        plugin_service._write_json_file(plugin_service.INSTALLED_FILE, {
            "version": 2,
            "plugins": {
                "test-plugin@test-mp": {
                    "scope": "user",
                    "installPath": str(plugin_path),
                    "version": "1.0"
                }
            }
        })

        result = plugin_service.get_plugin_details("test-plugin@test-mp")

        assert result is not None
        assert result.id == "test-plugin@test-mp"

    def test_parse_agent_frontmatter_with_empty_content(self, plugin_service):
        """Should handle empty content."""
        result = plugin_service._parse_agent_frontmatter("")

        assert result["prompt"] == ""
        assert result["description"] == ""

    def test_parse_agent_frontmatter_only_dashes_at_start(self, plugin_service):
        """Should handle content that starts with --- but has no closing."""
        content = "---\ndescription: test\nno closing"

        result = plugin_service._parse_agent_frontmatter(content)

        # Without closing ---, entire content becomes prompt
        assert content in result["prompt"]

    def test_uninstall_plugin_empty_installations_array(self, plugin_service):
        """Should handle empty array in v2 format during uninstall."""
        plugin_service._write_json_file(plugin_service.INSTALLED_FILE, {
            "version": 2,
            "plugins": {
                "test-plugin@test-mp": []
            }
        })

        # Should still work even with empty array
        result = plugin_service.uninstall_plugin("test-plugin@test-mp")

        assert result is True

    def test_get_installed_plugins_v2_array_with_none_first_element(self, plugin_service):
        """Should skip plugins where v2 array has None as first element."""
        plugin_service._write_json_file(plugin_service.INSTALLED_FILE, {
            "version": 2,
            "plugins": {
                "null-element@test-mp": [None],
                "empty-dict@test-mp": [{}]
            }
        })

        result = plugin_service.get_installed_plugins()

        # Both should be skipped because first element is falsy
        assert len(result) == 0

    def test_get_plugin_details_v2_array_with_none_first_element(self, plugin_service):
        """Should return None when v2 array has None as first element."""
        plugin_service._write_json_file(plugin_service.INSTALLED_FILE, {
            "version": 2,
            "plugins": {
                "test-plugin@test-mp": [None]
            }
        })

        result = plugin_service.get_plugin_details("test-plugin@test-mp")

        assert result is None

    def test_get_plugin_details_v2_array_with_empty_dict(self, plugin_service):
        """Should return None when v2 array has empty dict as first element."""
        plugin_service._write_json_file(plugin_service.INSTALLED_FILE, {
            "version": 2,
            "plugins": {
                "test-plugin@test-mp": [{}]
            }
        })

        result = plugin_service.get_plugin_details("test-plugin@test-mp")

        # Empty dict is truthy, so it will try to access installPath
        # which returns "", and Path("") doesn't exist, so returns None
        assert result is None
