"""
Unit tests for cleanup manager module.

Tests cover:
- CleanupStats and FileCleanupPreview dataclasses
- Configuration loading and caching
- Sleep mode management
- File cleanup operations (scanning, preview, deletion)
- Cleanup cycle execution
- Project filtering by ID
- Error handling for I/O operations
- Concurrent cleanup behavior
- Retention policy enforcement
"""

import pytest
import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock
import tempfile
import os

from app.core.cleanup_manager import (
    CleanupManager,
    CleanupStats,
    FileCleanupPreview,
    DEFAULT_CONFIG,
    cleanup_manager,
)


class TestCleanupStats:
    """Test CleanupStats dataclass."""

    def test_default_values(self):
        """Default stats should be all zeros."""
        stats = CleanupStats()

        assert stats.sdk_sessions_cleaned == 0
        assert stats.websocket_connections_cleaned == 0
        assert stats.auth_sessions_cleaned == 0
        assert stats.sync_logs_cleaned == 0
        assert stats.images_deleted == 0
        assert stats.videos_deleted == 0
        assert stats.uploads_deleted == 0
        assert stats.bytes_freed == 0

    def test_custom_values(self):
        """Should accept custom values."""
        stats = CleanupStats(
            sdk_sessions_cleaned=5,
            websocket_connections_cleaned=10,
            auth_sessions_cleaned=3,
            sync_logs_cleaned=100,
            images_deleted=25,
            videos_deleted=15,
            uploads_deleted=8,
            bytes_freed=1024000
        )

        assert stats.sdk_sessions_cleaned == 5
        assert stats.websocket_connections_cleaned == 10
        assert stats.auth_sessions_cleaned == 3
        assert stats.sync_logs_cleaned == 100
        assert stats.images_deleted == 25
        assert stats.videos_deleted == 15
        assert stats.uploads_deleted == 8
        assert stats.bytes_freed == 1024000


class TestFileCleanupPreview:
    """Test FileCleanupPreview dataclass."""

    def test_default_values(self):
        """Default preview should have empty lists and zero totals."""
        preview = FileCleanupPreview()

        assert preview.images == []
        assert preview.videos == []
        assert preview.uploads == []
        assert preview.total_count == 0
        assert preview.total_bytes == 0

    def test_custom_values(self):
        """Should accept custom values."""
        preview = FileCleanupPreview(
            images=[{"name": "img1.png", "size": 1000}],
            videos=[{"name": "vid1.mp4", "size": 5000}],
            uploads=[{"name": "file1.txt", "size": 200}],
            total_count=3,
            total_bytes=6200
        )

        assert len(preview.images) == 1
        assert len(preview.videos) == 1
        assert len(preview.uploads) == 1
        assert preview.total_count == 3
        assert preview.total_bytes == 6200


class TestCleanupManagerInit:
    """Test CleanupManager initialization."""

    def test_initial_state(self):
        """Manager should start in a non-sleeping, active state."""
        manager = CleanupManager()

        assert manager._is_sleeping is False
        assert manager._config_cache == {}
        assert manager._config_loaded_at is None
        assert manager._cleanup_callbacks == []
        assert manager._last_activity is not None

    def test_last_activity_set_on_init(self):
        """Last activity should be set to current time on init."""
        before = datetime.utcnow()
        manager = CleanupManager()
        after = datetime.utcnow()

        assert before <= manager._last_activity <= after


class TestActivityTracking:
    """Test activity recording and idle time calculation."""

    def test_record_activity_updates_last_activity(self):
        """Recording activity should update last activity time."""
        manager = CleanupManager()
        old_activity = manager._last_activity

        # Wait a tiny bit to ensure time difference
        manager.record_activity()

        assert manager._last_activity >= old_activity

    def test_record_activity_wakes_from_sleep(self):
        """Recording activity should wake up sleeping manager."""
        manager = CleanupManager()
        manager._is_sleeping = True

        manager.record_activity()

        assert manager._is_sleeping is False

    def test_record_activity_when_not_sleeping(self):
        """Recording activity when not sleeping should not change state."""
        manager = CleanupManager()
        manager._is_sleeping = False

        manager.record_activity()

        assert manager._is_sleeping is False

    def test_get_idle_seconds(self):
        """Should return seconds since last activity."""
        manager = CleanupManager()
        manager._last_activity = datetime.utcnow() - timedelta(seconds=30)

        idle = manager.get_idle_seconds()

        assert 29 <= idle <= 31  # Allow for small timing variations

    def test_is_sleeping_returns_state(self):
        """is_sleeping() should return current sleep state."""
        manager = CleanupManager()

        assert manager.is_sleeping() is False

        manager._is_sleeping = True
        assert manager.is_sleeping() is True


class TestWakeUp:
    """Test wake up functionality."""

    def test_wake_up_from_sleeping_state(self):
        """Wake up should set is_sleeping to False."""
        manager = CleanupManager()
        manager._is_sleeping = True

        manager._wake_up()

        assert manager._is_sleeping is False

    def test_wake_up_when_already_awake(self):
        """Wake up when not sleeping should do nothing."""
        manager = CleanupManager()
        manager._is_sleeping = False

        manager._wake_up()

        assert manager._is_sleeping is False


class TestConfigLoading:
    """Test configuration loading from database."""

    @pytest.fixture
    def mock_database(self):
        """Create a mock database module."""
        with patch("app.core.cleanup_manager.database") as mock_db:
            yield mock_db

    def test_get_config_loads_from_cache(self, mock_database):
        """Should use cached config if available and not stale."""
        manager = CleanupManager()
        manager._config_cache = {"cleanup_interval_minutes": 10}
        manager._config_loaded_at = datetime.utcnow()

        result = manager.get_config("cleanup_interval_minutes")

        assert result == 10
        mock_database.get_system_setting.assert_not_called()

    def test_get_config_refreshes_stale_cache(self, mock_database):
        """Should reload config if cache is older than 60 seconds."""
        mock_database.get_system_setting.return_value = None

        manager = CleanupManager()
        manager._config_cache = {"cleanup_interval_minutes": 10}
        manager._config_loaded_at = datetime.utcnow() - timedelta(seconds=61)

        manager.get_config("cleanup_interval_minutes")

        mock_database.get_system_setting.assert_called()

    def test_get_config_returns_default_for_missing_key(self, mock_database):
        """Should return default value for missing config key."""
        mock_database.get_system_setting.return_value = None

        manager = CleanupManager()

        result = manager.get_config("cleanup_interval_minutes")

        assert result == DEFAULT_CONFIG["cleanup_interval_minutes"]

    def test_get_config_with_explicit_default(self, mock_database):
        """Should use provided default if key not in cache or defaults."""
        mock_database.get_system_setting.return_value = None

        manager = CleanupManager()

        result = manager.get_config("nonexistent_key", default=42)

        assert result == 42

    def test_load_config_parses_boolean_true(self, mock_database):
        """Should parse 'true' string to boolean True."""
        def get_setting(key):
            if key == "cleanup_sleep_mode_enabled":
                return "true"
            return None

        mock_database.get_system_setting.side_effect = get_setting

        manager = CleanupManager()
        manager._load_config()

        assert manager._config_cache["sleep_mode_enabled"] is True

    def test_load_config_parses_boolean_false(self, mock_database):
        """Should parse 'false' string to boolean False."""
        def get_setting(key):
            if key == "cleanup_sleep_mode_enabled":
                return "false"
            return None

        mock_database.get_system_setting.side_effect = get_setting

        manager = CleanupManager()
        manager._load_config()

        assert manager._config_cache["sleep_mode_enabled"] is False

    def test_load_config_parses_integer(self, mock_database):
        """Should parse integer values from string."""
        def get_setting(key):
            if key == "cleanup_cleanup_interval_minutes":
                return "15"
            return None

        mock_database.get_system_setting.side_effect = get_setting

        manager = CleanupManager()
        manager._load_config()

        assert manager._config_cache["cleanup_interval_minutes"] == 15

    def test_load_config_parses_json_list(self, mock_database):
        """Should parse JSON for list values."""
        def get_setting(key):
            if key == "cleanup_cleanup_project_ids":
                return '["project1", "project2"]'
            return None

        mock_database.get_system_setting.side_effect = get_setting

        manager = CleanupManager()
        manager._load_config()

        assert manager._config_cache["cleanup_project_ids"] == ["project1", "project2"]

    def test_load_config_handles_invalid_json(self, mock_database):
        """Should use default for invalid JSON."""
        def get_setting(key):
            if key == "cleanup_cleanup_project_ids":
                return "invalid json {"
            return None

        mock_database.get_system_setting.side_effect = get_setting

        manager = CleanupManager()
        manager._load_config()

        # Should fall back to default
        assert manager._config_cache["cleanup_project_ids"] == []

    def test_load_config_handles_database_error(self, mock_database):
        """Should handle database errors gracefully."""
        mock_database.get_system_setting.side_effect = Exception("Database error")

        manager = CleanupManager()

        # Should not raise
        manager._load_config()

        # Config cache might be empty but should not crash
        assert True

    def test_load_config_sets_loaded_at_time(self, mock_database):
        """Should update config_loaded_at timestamp."""
        mock_database.get_system_setting.return_value = None

        manager = CleanupManager()
        before = datetime.utcnow()
        manager._load_config()
        after = datetime.utcnow()

        assert before <= manager._config_loaded_at <= after


class TestConfigSetting:
    """Test configuration setting to database."""

    @pytest.fixture
    def mock_database(self):
        """Create a mock database module."""
        with patch("app.core.cleanup_manager.database") as mock_db:
            yield mock_db

    def test_set_config_stores_boolean_true(self, mock_database):
        """Should store boolean True as 'true' string."""
        manager = CleanupManager()

        manager.set_config("sleep_mode_enabled", True)

        mock_database.set_system_setting.assert_called_with(
            "cleanup_sleep_mode_enabled", "true"
        )
        assert manager._config_cache["sleep_mode_enabled"] is True

    def test_set_config_stores_boolean_false(self, mock_database):
        """Should store boolean False as 'false' string."""
        manager = CleanupManager()

        manager.set_config("sleep_mode_enabled", False)

        mock_database.set_system_setting.assert_called_with(
            "cleanup_sleep_mode_enabled", "false"
        )
        assert manager._config_cache["sleep_mode_enabled"] is False

    def test_set_config_stores_list_as_json(self, mock_database):
        """Should store list as JSON string."""
        manager = CleanupManager()

        manager.set_config("cleanup_project_ids", ["proj1", "proj2"])

        mock_database.set_system_setting.assert_called_with(
            "cleanup_cleanup_project_ids", '["proj1", "proj2"]'
        )
        assert manager._config_cache["cleanup_project_ids"] == ["proj1", "proj2"]

    def test_set_config_stores_dict_as_json(self, mock_database):
        """Should store dict as JSON string."""
        manager = CleanupManager()
        test_dict = {"key": "value"}

        manager.set_config("some_dict", test_dict)

        mock_database.set_system_setting.assert_called_with(
            "cleanup_some_dict", '{"key": "value"}'
        )

    def test_set_config_stores_integer(self, mock_database):
        """Should store integer as string."""
        manager = CleanupManager()

        manager.set_config("cleanup_interval_minutes", 15)

        mock_database.set_system_setting.assert_called_with(
            "cleanup_cleanup_interval_minutes", "15"
        )


class TestGetAllConfig:
    """Test getting all configuration."""

    @pytest.fixture
    def mock_database(self):
        """Create a mock database module."""
        with patch("app.core.cleanup_manager.database") as mock_db:
            mock_db.get_system_setting.return_value = None
            yield mock_db

    def test_get_all_config_returns_defaults(self, mock_database):
        """Should return all defaults merged with cache."""
        manager = CleanupManager()

        config = manager.get_all_config()

        # Should have all default keys
        for key in DEFAULT_CONFIG:
            assert key in config

    def test_get_all_config_includes_cached_values(self, mock_database):
        """Should include cached overrides from database."""
        def get_setting(key):
            if key == "cleanup_cleanup_interval_minutes":
                return "99"
            return None

        mock_database.get_system_setting.side_effect = get_setting

        manager = CleanupManager()

        config = manager.get_all_config()

        # Should use value from database
        assert config["cleanup_interval_minutes"] == 99


class TestSleepMode:
    """Test sleep mode functionality."""

    @pytest.fixture
    def mock_database(self):
        """Create a mock database module."""
        with patch("app.core.cleanup_manager.database") as mock_db:
            mock_db.get_system_setting.return_value = None
            yield mock_db

    def test_should_sleep_when_idle_exceeds_timeout(self, mock_database):
        """Should return True when idle time exceeds timeout."""
        manager = CleanupManager()
        # Set last activity to more than 10 minutes ago (default timeout)
        manager._last_activity = datetime.utcnow() - timedelta(minutes=15)

        assert manager.should_sleep() is True

    def test_should_not_sleep_when_recently_active(self, mock_database):
        """Should return False when recently active."""
        manager = CleanupManager()
        manager._last_activity = datetime.utcnow() - timedelta(minutes=1)

        assert manager.should_sleep() is False

    def test_should_not_sleep_when_disabled(self, mock_database):
        """Should return False when sleep mode is disabled."""
        manager = CleanupManager()
        manager._config_cache["sleep_mode_enabled"] = False
        manager._config_loaded_at = datetime.utcnow()
        manager._last_activity = datetime.utcnow() - timedelta(minutes=100)

        assert manager.should_sleep() is False

    @pytest.mark.asyncio
    async def test_check_sleep_mode_enters_sleep(self, mock_database):
        """Should enter sleep mode when conditions are met."""
        manager = CleanupManager()
        manager._last_activity = datetime.utcnow() - timedelta(minutes=15)

        await manager.check_sleep_mode()

        assert manager._is_sleeping is True

    @pytest.mark.asyncio
    async def test_check_sleep_mode_already_sleeping(self, mock_database):
        """Should not re-enter sleep mode if already sleeping."""
        manager = CleanupManager()
        manager._is_sleeping = True
        manager._last_activity = datetime.utcnow() - timedelta(minutes=15)

        await manager.check_sleep_mode()

        # Should still be sleeping (no change)
        assert manager._is_sleeping is True


class TestProjectsToClean:
    """Test project filtering for cleanup."""

    @pytest.fixture
    def mock_database(self):
        """Create a mock database module."""
        with patch("app.core.cleanup_manager.database") as mock_db:
            mock_db.get_system_setting.return_value = None
            yield mock_db

    def test_get_all_projects_when_no_filter(self, mock_database):
        """Should return all projects when no filter is set."""
        mock_database.get_all_projects.return_value = [
            {"id": "proj1", "name": "Project 1", "path": "/path1"},
            {"id": "proj2", "name": "Project 2", "path": "/path2"},
        ]

        manager = CleanupManager()
        projects = manager.get_projects_to_clean()

        assert len(projects) == 2
        mock_database.get_all_projects.assert_called_once()

    def test_get_filtered_projects(self, mock_database):
        """Should return only selected projects when filter is set."""
        mock_database.get_all_projects.return_value = [
            {"id": "proj1", "name": "Project 1", "path": "/path1"},
            {"id": "proj2", "name": "Project 2", "path": "/path2"},
            {"id": "proj3", "name": "Project 3", "path": "/path3"},
        ]

        manager = CleanupManager()
        manager._config_cache["cleanup_project_ids"] = ["proj1", "proj3"]
        manager._config_loaded_at = datetime.utcnow()

        projects = manager.get_projects_to_clean()

        assert len(projects) == 2
        assert projects[0]["id"] == "proj1"
        assert projects[1]["id"] == "proj3"


class TestGetGeneratedFolders:
    """Test generated folder path construction."""

    def test_get_generated_folders(self):
        """Should return correct folder paths."""
        manager = CleanupManager()
        project_path = Path("/test/project")

        folders = manager._get_generated_folders(project_path)

        assert folders["images"] == Path("/test/project/generated-images")
        assert folders["videos"] == Path("/test/project/generated-videos")
        assert folders["uploads"] == Path("/test/project/uploads")


class TestScanOldFiles:
    """Test file scanning for old files."""

    def test_scan_empty_folder(self, temp_dir):
        """Should return empty list for empty folder."""
        manager = CleanupManager()
        empty_folder = temp_dir / "empty"
        empty_folder.mkdir()

        files = manager._scan_old_files(empty_folder, max_age_days=7)

        assert files == []

    def test_scan_nonexistent_folder(self, temp_dir):
        """Should return empty list for nonexistent folder."""
        manager = CleanupManager()
        nonexistent = temp_dir / "nonexistent"

        files = manager._scan_old_files(nonexistent, max_age_days=7)

        assert files == []

    def test_scan_finds_old_files(self, temp_dir):
        """Should find files older than max_age_days."""
        manager = CleanupManager()
        folder = temp_dir / "old_files"
        folder.mkdir()

        # Create an old file
        old_file = folder / "old.txt"
        old_file.write_text("old content")
        # Set modification time to 10 days ago
        old_time = (datetime.now() - timedelta(days=10)).timestamp()
        os.utime(old_file, (old_time, old_time))

        files = manager._scan_old_files(folder, max_age_days=7)

        assert len(files) == 1
        assert files[0]["name"] == "old.txt"
        assert files[0]["age_days"] >= 10

    def test_scan_ignores_new_files(self, temp_dir):
        """Should not include files newer than max_age_days."""
        manager = CleanupManager()
        folder = temp_dir / "new_files"
        folder.mkdir()

        # Create a new file
        new_file = folder / "new.txt"
        new_file.write_text("new content")

        files = manager._scan_old_files(folder, max_age_days=7)

        assert len(files) == 0

    def test_scan_includes_file_info(self, temp_dir):
        """Should include path, name, size, modified, and age_days."""
        manager = CleanupManager()
        folder = temp_dir / "files"
        folder.mkdir()

        # Create an old file
        old_file = folder / "test.txt"
        old_file.write_text("test content")
        old_time = (datetime.now() - timedelta(days=10)).timestamp()
        os.utime(old_file, (old_time, old_time))

        files = manager._scan_old_files(folder, max_age_days=7)

        assert len(files) == 1
        assert "path" in files[0]
        assert "name" in files[0]
        assert "size" in files[0]
        assert "modified" in files[0]
        assert "age_days" in files[0]
        assert files[0]["name"] == "test.txt"
        assert files[0]["size"] == 12  # len("test content")

    def test_scan_ignores_directories(self, temp_dir):
        """Should only include files, not directories."""
        manager = CleanupManager()
        folder = temp_dir / "mixed"
        folder.mkdir()

        # Create a subdirectory
        subdir = folder / "subdir"
        subdir.mkdir()

        files = manager._scan_old_files(folder, max_age_days=0)

        assert len(files) == 0

    def test_scan_handles_permission_error(self, temp_dir):
        """Should handle errors gracefully."""
        manager = CleanupManager()

        # Create a folder with a file that will cause an error
        folder = temp_dir / "error_folder"
        folder.mkdir()

        # Mock iterdir to raise an error
        with patch.object(Path, "iterdir", side_effect=PermissionError("Access denied")):
            files = manager._scan_old_files(folder, max_age_days=7)

        assert files == []


class TestDeleteFiles:
    """Test file deletion functionality."""

    def test_delete_existing_files(self, temp_dir):
        """Should delete files and return count and bytes freed."""
        manager = CleanupManager()

        # Create test files
        file1 = temp_dir / "file1.txt"
        file1.write_text("content1")
        file2 = temp_dir / "file2.txt"
        file2.write_text("content2")

        files_info = [
            {"path": str(file1), "size": 8},
            {"path": str(file2), "size": 8},
        ]

        count, bytes_freed = manager._delete_files(files_info)

        assert count == 2
        assert bytes_freed == 16
        assert not file1.exists()
        assert not file2.exists()

    def test_delete_nonexistent_file(self, temp_dir):
        """Should skip nonexistent files."""
        manager = CleanupManager()

        files_info = [
            {"path": str(temp_dir / "nonexistent.txt"), "size": 100},
        ]

        count, bytes_freed = manager._delete_files(files_info)

        assert count == 0
        assert bytes_freed == 0

    def test_delete_handles_permission_error(self, temp_dir):
        """Should handle deletion errors gracefully."""
        manager = CleanupManager()

        # Create a file
        test_file = temp_dir / "protected.txt"
        test_file.write_text("protected")

        files_info = [{"path": str(test_file), "size": 9}]

        # Mock unlink to raise an error
        with patch.object(Path, "unlink", side_effect=PermissionError("Access denied")):
            count, bytes_freed = manager._delete_files(files_info)

        assert count == 0
        assert bytes_freed == 0


class TestPreviewFileCleanup:
    """Test file cleanup preview functionality."""

    @pytest.fixture
    def mock_database(self):
        """Create a mock database module."""
        with patch("app.core.cleanup_manager.database") as mock_db:
            mock_db.get_system_setting.return_value = None
            mock_db.get_all_projects.return_value = []
            yield mock_db

    @pytest.fixture
    def mock_settings(self, temp_dir):
        """Mock settings with temp directory."""
        with patch("app.core.cleanup_manager.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir
            yield mock_settings

    def test_preview_with_no_enabled_cleanup(self, mock_database, mock_settings, temp_dir):
        """Should return empty preview when no cleanup is enabled."""
        manager = CleanupManager()

        preview = manager.preview_file_cleanup()

        assert preview.images == []
        assert preview.videos == []
        assert preview.uploads == []
        assert preview.total_count == 0
        assert preview.total_bytes == 0

    def test_preview_with_images_enabled(self, mock_database, mock_settings, temp_dir):
        """Should include images in preview when enabled."""
        # Enable image cleanup
        manager = CleanupManager()
        manager._config_cache = {
            "cleanup_images_enabled": True,
            "cleanup_images_max_age_days": 7,
            "cleanup_videos_enabled": False,
            "cleanup_uploads_enabled": False,
            "cleanup_project_ids": [],
        }
        manager._config_loaded_at = datetime.utcnow()

        # Create generated-images folder with old file
        images_folder = temp_dir / "generated-images"
        images_folder.mkdir()
        old_image = images_folder / "old.png"
        old_image.write_text("image data")
        old_time = (datetime.now() - timedelta(days=10)).timestamp()
        os.utime(old_image, (old_time, old_time))

        preview = manager.preview_file_cleanup()

        assert len(preview.images) == 1
        assert preview.images[0]["name"] == "old.png"
        assert preview.images[0]["project"] == "(workspace root)"
        assert preview.total_count == 1

    def test_preview_includes_project_files(self, mock_database, mock_settings, temp_dir):
        """Should include files from projects."""
        # Set up project
        project_path = temp_dir / "my-project"
        project_path.mkdir()
        images_folder = project_path / "generated-images"
        images_folder.mkdir()

        mock_database.get_all_projects.return_value = [
            {"id": "proj1", "name": "My Project", "path": "my-project"}
        ]

        manager = CleanupManager()
        manager._config_cache = {
            "cleanup_images_enabled": True,
            "cleanup_images_max_age_days": 7,
            "cleanup_videos_enabled": False,
            "cleanup_uploads_enabled": False,
            "cleanup_project_ids": [],
        }
        manager._config_loaded_at = datetime.utcnow()

        # Create old image in project
        old_image = images_folder / "project_image.png"
        old_image.write_text("project image data")
        old_time = (datetime.now() - timedelta(days=10)).timestamp()
        os.utime(old_image, (old_time, old_time))

        preview = manager.preview_file_cleanup()

        # Should include project image
        project_images = [f for f in preview.images if f["project"] == "My Project"]
        assert len(project_images) == 1

    def test_preview_calculates_totals(self, mock_database, mock_settings, temp_dir):
        """Should calculate total count and bytes correctly."""
        manager = CleanupManager()
        manager._config_cache = {
            "cleanup_images_enabled": True,
            "cleanup_images_max_age_days": 7,
            "cleanup_videos_enabled": True,
            "cleanup_videos_max_age_days": 7,
            "cleanup_uploads_enabled": True,
            "cleanup_uploads_max_age_days": 7,
            "cleanup_project_ids": [],
        }
        manager._config_loaded_at = datetime.utcnow()

        # Create folders with old files
        for folder_name in ["generated-images", "generated-videos", "uploads"]:
            folder = temp_dir / folder_name
            folder.mkdir()
            old_file = folder / f"old_{folder_name}.dat"
            old_file.write_text("x" * 100)  # 100 bytes each
            old_time = (datetime.now() - timedelta(days=10)).timestamp()
            os.utime(old_file, (old_time, old_time))

        preview = manager.preview_file_cleanup()

        assert preview.total_count == 3
        assert preview.total_bytes == 300


class TestRunFileCleanup:
    """Test file cleanup execution."""

    @pytest.fixture
    def mock_database(self):
        """Create a mock database module."""
        with patch("app.core.cleanup_manager.database") as mock_db:
            mock_db.get_system_setting.return_value = None
            mock_db.get_all_projects.return_value = []
            yield mock_db

    @pytest.fixture
    def mock_settings(self, temp_dir):
        """Mock settings with temp directory."""
        with patch("app.core.cleanup_manager.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir
            yield mock_settings

    @pytest.mark.asyncio
    async def test_run_file_cleanup_deletes_files(self, mock_database, mock_settings, temp_dir):
        """Should delete files and return stats."""
        manager = CleanupManager()
        manager._config_cache = {
            "cleanup_images_enabled": True,
            "cleanup_images_max_age_days": 7,
            "cleanup_videos_enabled": False,
            "cleanup_uploads_enabled": False,
            "cleanup_project_ids": [],
        }
        manager._config_loaded_at = datetime.utcnow()

        # Create folder with old file
        images_folder = temp_dir / "generated-images"
        images_folder.mkdir()
        old_image = images_folder / "old.png"
        old_image.write_text("image data")
        old_time = (datetime.now() - timedelta(days=10)).timestamp()
        os.utime(old_image, (old_time, old_time))

        stats = await manager.run_file_cleanup()

        assert stats.images_deleted == 1
        assert stats.bytes_freed > 0
        assert not old_image.exists()

    @pytest.mark.asyncio
    async def test_run_file_cleanup_returns_empty_stats_when_nothing_to_delete(
        self, mock_database, mock_settings, temp_dir
    ):
        """Should return zero stats when no files to delete."""
        manager = CleanupManager()
        manager._config_cache = {
            "cleanup_images_enabled": True,
            "cleanup_images_max_age_days": 7,
            "cleanup_videos_enabled": False,
            "cleanup_uploads_enabled": False,
            "cleanup_project_ids": [],
        }
        manager._config_loaded_at = datetime.utcnow()

        stats = await manager.run_file_cleanup()

        assert stats.images_deleted == 0
        assert stats.videos_deleted == 0
        assert stats.uploads_deleted == 0
        assert stats.bytes_freed == 0


class TestRunCleanupCycle:
    """Test full cleanup cycle execution."""

    @pytest.fixture
    def mock_database(self):
        """Create a mock database module."""
        with patch("app.core.cleanup_manager.database") as mock_db:
            mock_db.get_system_setting.return_value = None
            mock_db.get_all_projects.return_value = []
            mock_db.cleanup_expired_sessions = MagicMock()
            mock_db.cleanup_expired_lockouts = MagicMock()
            mock_db.cleanup_expired_api_key_sessions = MagicMock()
            mock_db.cleanup_old_sync_logs = MagicMock()
            mock_db.cleanup_old_login_attempts = MagicMock()
            yield mock_db

    @pytest.fixture
    def mock_settings(self, temp_dir):
        """Mock settings with temp directory."""
        with patch("app.core.cleanup_manager.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir
            yield mock_settings

    @pytest.mark.asyncio
    async def test_run_cleanup_cycle_calls_callbacks(self, mock_database, mock_settings):
        """Should call provided cleanup callbacks."""
        manager = CleanupManager()
        manager._config_cache = {
            "cleanup_images_enabled": False,
            "cleanup_videos_enabled": False,
            "cleanup_uploads_enabled": False,
            "cleanup_project_ids": [],
            "sdk_session_max_age_minutes": 60,
            "websocket_max_age_minutes": 5,
            "sync_log_retention_hours": 24,
            "sleep_mode_enabled": False,
        }
        manager._config_loaded_at = datetime.utcnow()

        sessions_callback = AsyncMock()
        connections_callback = AsyncMock()

        await manager.run_cleanup_cycle(
            cleanup_sessions_callback=sessions_callback,
            cleanup_connections_callback=connections_callback,
        )

        sessions_callback.assert_called_once_with(max_age_seconds=3600)  # 60 * 60
        connections_callback.assert_called_once_with(max_age_seconds=300)  # 5 * 60

    @pytest.mark.asyncio
    async def test_run_cleanup_cycle_calls_database_cleanup(self, mock_database, mock_settings):
        """Should call database cleanup functions."""
        manager = CleanupManager()
        manager._config_cache = {
            "cleanup_images_enabled": False,
            "cleanup_videos_enabled": False,
            "cleanup_uploads_enabled": False,
            "cleanup_project_ids": [],
            "sdk_session_max_age_minutes": 60,
            "websocket_max_age_minutes": 5,
            "sync_log_retention_hours": 24,
            "sleep_mode_enabled": False,
        }
        manager._config_loaded_at = datetime.utcnow()

        await manager.run_cleanup_cycle()

        mock_database.cleanup_expired_sessions.assert_called_once()
        mock_database.cleanup_expired_lockouts.assert_called_once()
        mock_database.cleanup_expired_api_key_sessions.assert_called_once()
        mock_database.cleanup_old_sync_logs.assert_called_once_with(max_age_hours=24)
        mock_database.cleanup_old_login_attempts.assert_called_once_with(max_age_hours=24)

    @pytest.mark.asyncio
    async def test_run_cleanup_cycle_skips_when_sleeping(self, mock_database, mock_settings):
        """Should skip cleanup cycle when app is sleeping."""
        manager = CleanupManager()
        manager._is_sleeping = True
        manager._config_cache = {
            "sleep_mode_enabled": True,
        }
        manager._config_loaded_at = datetime.utcnow()

        sessions_callback = AsyncMock()

        stats = await manager.run_cleanup_cycle(
            cleanup_sessions_callback=sessions_callback,
        )

        sessions_callback.assert_not_called()
        mock_database.cleanup_expired_sessions.assert_not_called()
        assert stats.images_deleted == 0

    @pytest.mark.asyncio
    async def test_run_cleanup_cycle_checks_sleep_mode(self, mock_database, mock_settings):
        """Should check and enter sleep mode if appropriate."""
        manager = CleanupManager()
        manager._last_activity = datetime.utcnow() - timedelta(minutes=15)
        manager._config_cache = {
            "cleanup_images_enabled": False,
            "cleanup_videos_enabled": False,
            "cleanup_uploads_enabled": False,
            "cleanup_project_ids": [],
            "sdk_session_max_age_minutes": 60,
            "websocket_max_age_minutes": 5,
            "sync_log_retention_hours": 24,
            "sleep_mode_enabled": True,
            "sleep_timeout_minutes": 10,
        }
        manager._config_loaded_at = datetime.utcnow()

        await manager.run_cleanup_cycle()

        assert manager._is_sleeping is True

    @pytest.mark.asyncio
    async def test_run_cleanup_cycle_includes_file_stats(self, mock_database, mock_settings, temp_dir):
        """Should include file cleanup stats in result."""
        manager = CleanupManager()
        manager._config_cache = {
            "cleanup_images_enabled": True,
            "cleanup_images_max_age_days": 7,
            "cleanup_videos_enabled": False,
            "cleanup_uploads_enabled": False,
            "cleanup_project_ids": [],
            "sdk_session_max_age_minutes": 60,
            "websocket_max_age_minutes": 5,
            "sync_log_retention_hours": 24,
            "sleep_mode_enabled": False,
        }
        manager._config_loaded_at = datetime.utcnow()

        # Create folder with old file
        images_folder = temp_dir / "generated-images"
        images_folder.mkdir()
        old_image = images_folder / "old.png"
        old_image.write_text("image data")
        old_time = (datetime.now() - timedelta(days=10)).timestamp()
        os.utime(old_image, (old_time, old_time))

        stats = await manager.run_cleanup_cycle()

        assert stats.images_deleted == 1
        assert stats.bytes_freed > 0


class TestGetStatus:
    """Test status reporting."""

    @pytest.fixture
    def mock_database(self):
        """Create a mock database module."""
        with patch("app.core.cleanup_manager.database") as mock_db:
            mock_db.get_system_setting.return_value = None
            yield mock_db

    def test_get_status_returns_all_fields(self, mock_database):
        """Should return all status fields."""
        manager = CleanupManager()
        manager._config_cache = {
            "sleep_mode_enabled": True,
            "sleep_timeout_minutes": 10,
            "cleanup_interval_minutes": 5,
        }
        manager._config_loaded_at = datetime.utcnow()

        status = manager.get_status()

        assert "is_sleeping" in status
        assert "idle_seconds" in status
        assert "last_activity" in status
        assert "sleep_mode_enabled" in status
        assert "sleep_timeout_minutes" in status
        assert "cleanup_interval_minutes" in status

    def test_get_status_reflects_sleep_state(self, mock_database):
        """Should reflect current sleep state."""
        manager = CleanupManager()
        manager._is_sleeping = True
        manager._config_cache = {
            "sleep_mode_enabled": True,
            "sleep_timeout_minutes": 10,
            "cleanup_interval_minutes": 5,
        }
        manager._config_loaded_at = datetime.utcnow()

        status = manager.get_status()

        assert status["is_sleeping"] is True


class TestGlobalInstance:
    """Test global cleanup_manager instance."""

    def test_global_instance_exists(self):
        """Should have a global cleanup_manager instance."""
        assert cleanup_manager is not None
        assert isinstance(cleanup_manager, CleanupManager)


class TestDefaultConfig:
    """Test DEFAULT_CONFIG values."""

    def test_default_config_contains_all_keys(self):
        """Should have all required configuration keys."""
        required_keys = [
            "cleanup_interval_minutes",
            "sdk_session_max_age_minutes",
            "websocket_max_age_minutes",
            "sync_log_retention_hours",
            "cleanup_images_enabled",
            "cleanup_images_max_age_days",
            "cleanup_videos_enabled",
            "cleanup_videos_max_age_days",
            "cleanup_uploads_enabled",
            "cleanup_uploads_max_age_days",
            "cleanup_project_ids",
            "sleep_mode_enabled",
            "sleep_timeout_minutes",
        ]

        for key in required_keys:
            assert key in DEFAULT_CONFIG

    def test_default_config_has_reasonable_values(self):
        """Should have reasonable default values."""
        assert DEFAULT_CONFIG["cleanup_interval_minutes"] > 0
        assert DEFAULT_CONFIG["sdk_session_max_age_minutes"] > 0
        assert DEFAULT_CONFIG["websocket_max_age_minutes"] > 0
        assert DEFAULT_CONFIG["sync_log_retention_hours"] > 0
        assert DEFAULT_CONFIG["cleanup_images_max_age_days"] > 0
        assert DEFAULT_CONFIG["cleanup_videos_max_age_days"] > 0
        assert DEFAULT_CONFIG["cleanup_uploads_max_age_days"] > 0
        assert DEFAULT_CONFIG["sleep_timeout_minutes"] > 0

    def test_default_config_file_cleanup_disabled(self):
        """File cleanup should be disabled by default."""
        assert DEFAULT_CONFIG["cleanup_images_enabled"] is False
        assert DEFAULT_CONFIG["cleanup_videos_enabled"] is False
        assert DEFAULT_CONFIG["cleanup_uploads_enabled"] is False

    def test_default_config_sleep_mode_enabled(self):
        """Sleep mode should be enabled by default."""
        assert DEFAULT_CONFIG["sleep_mode_enabled"] is True


class TestRetentionPolicies:
    """Test retention policy enforcement."""

    @pytest.fixture
    def mock_database(self):
        """Create a mock database module."""
        with patch("app.core.cleanup_manager.database") as mock_db:
            mock_db.get_system_setting.return_value = None
            mock_db.get_all_projects.return_value = []
            yield mock_db

    @pytest.fixture
    def mock_settings(self, temp_dir):
        """Mock settings with temp directory."""
        with patch("app.core.cleanup_manager.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir
            yield mock_settings

    def test_files_newer_than_max_age_are_not_deleted(self, mock_database, mock_settings, temp_dir):
        """Files newer than max_age_days should NOT be deleted."""
        manager = CleanupManager()
        manager._config_cache = {
            "cleanup_images_enabled": True,
            "cleanup_images_max_age_days": 7,
            "cleanup_videos_enabled": False,
            "cleanup_uploads_enabled": False,
            "cleanup_project_ids": [],
        }
        manager._config_loaded_at = datetime.utcnow()

        # Create folder with file that is 6 days old (newer than 7 day cutoff)
        images_folder = temp_dir / "generated-images"
        images_folder.mkdir()
        recent_file = images_folder / "recent.png"
        recent_file.write_text("recent image data")
        # Set to 6 days ago (should NOT be deleted - within 7 day retention)
        recent_time = (datetime.now() - timedelta(days=6)).timestamp()
        os.utime(recent_file, (recent_time, recent_time))

        preview = manager.preview_file_cleanup()

        # File newer than max_age should NOT be included
        assert len(preview.images) == 0

    def test_files_older_than_max_age_are_deleted(self, mock_database, mock_settings, temp_dir):
        """Files older than max_age_days should be deleted."""
        manager = CleanupManager()
        manager._config_cache = {
            "cleanup_images_enabled": True,
            "cleanup_images_max_age_days": 7,
            "cleanup_videos_enabled": False,
            "cleanup_uploads_enabled": False,
            "cleanup_project_ids": [],
        }
        manager._config_loaded_at = datetime.utcnow()

        # Create folder with file older than 7 days
        images_folder = temp_dir / "generated-images"
        images_folder.mkdir()
        old_file = images_folder / "old.png"
        old_file.write_text("old image data")
        old_time = (datetime.now() - timedelta(days=8)).timestamp()
        os.utime(old_file, (old_time, old_time))

        preview = manager.preview_file_cleanup()

        assert len(preview.images) == 1

    def test_different_max_ages_per_file_type(self, mock_database, mock_settings, temp_dir):
        """Should use different max ages for different file types."""
        manager = CleanupManager()
        manager._config_cache = {
            "cleanup_images_enabled": True,
            "cleanup_images_max_age_days": 3,
            "cleanup_videos_enabled": True,
            "cleanup_videos_max_age_days": 30,
            "cleanup_uploads_enabled": False,
            "cleanup_project_ids": [],
        }
        manager._config_loaded_at = datetime.utcnow()

        # Create images folder with 5-day-old file (should be cleaned with 3-day policy)
        images_folder = temp_dir / "generated-images"
        images_folder.mkdir()
        old_image = images_folder / "old.png"
        old_image.write_text("old image")
        old_time = (datetime.now() - timedelta(days=5)).timestamp()
        os.utime(old_image, (old_time, old_time))

        # Create videos folder with 5-day-old file (should NOT be cleaned with 30-day policy)
        videos_folder = temp_dir / "generated-videos"
        videos_folder.mkdir()
        old_video = videos_folder / "old.mp4"
        old_video.write_text("old video")
        os.utime(old_video, (old_time, old_time))

        preview = manager.preview_file_cleanup()

        assert len(preview.images) == 1  # 5 days > 3 days policy
        assert len(preview.videos) == 0  # 5 days < 30 days policy


class TestConcurrentCleanup:
    """Test concurrent cleanup scenarios."""

    @pytest.fixture
    def mock_database(self):
        """Create a mock database module."""
        with patch("app.core.cleanup_manager.database") as mock_db:
            mock_db.get_system_setting.return_value = None
            mock_db.get_all_projects.return_value = []
            mock_db.cleanup_expired_sessions = MagicMock()
            mock_db.cleanup_expired_lockouts = MagicMock()
            mock_db.cleanup_expired_api_key_sessions = MagicMock()
            mock_db.cleanup_old_sync_logs = MagicMock()
            mock_db.cleanup_old_login_attempts = MagicMock()
            yield mock_db

    @pytest.fixture
    def mock_settings(self, temp_dir):
        """Mock settings with temp directory."""
        with patch("app.core.cleanup_manager.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir
            yield mock_settings

    @pytest.mark.asyncio
    async def test_activity_during_cleanup_wakes_manager(self, mock_database, mock_settings):
        """Recording activity should wake manager even during cleanup."""
        manager = CleanupManager()
        manager._config_cache = {
            "cleanup_images_enabled": False,
            "cleanup_videos_enabled": False,
            "cleanup_uploads_enabled": False,
            "cleanup_project_ids": [],
            "sdk_session_max_age_minutes": 60,
            "websocket_max_age_minutes": 5,
            "sync_log_retention_hours": 24,
            "sleep_mode_enabled": True,
            "sleep_timeout_minutes": 10,
        }
        manager._config_loaded_at = datetime.utcnow()

        # Put manager to sleep
        manager._is_sleeping = True

        # Record activity (simulating user interaction)
        manager.record_activity()

        # Should wake up
        assert manager._is_sleeping is False

    @pytest.mark.asyncio
    async def test_multiple_cleanup_cycles(self, mock_database, mock_settings, temp_dir):
        """Should handle multiple cleanup cycles correctly."""
        manager = CleanupManager()
        manager._config_cache = {
            "cleanup_images_enabled": True,
            "cleanup_images_max_age_days": 7,
            "cleanup_videos_enabled": False,
            "cleanup_uploads_enabled": False,
            "cleanup_project_ids": [],
            "sdk_session_max_age_minutes": 60,
            "websocket_max_age_minutes": 5,
            "sync_log_retention_hours": 24,
            "sleep_mode_enabled": False,
        }
        manager._config_loaded_at = datetime.utcnow()

        # Create folder with old file
        images_folder = temp_dir / "generated-images"
        images_folder.mkdir()
        old_image = images_folder / "old.png"
        old_image.write_text("image data")
        old_time = (datetime.now() - timedelta(days=10)).timestamp()
        os.utime(old_image, (old_time, old_time))

        # Run first cleanup cycle
        stats1 = await manager.run_cleanup_cycle()
        assert stats1.images_deleted == 1

        # Run second cleanup cycle (should have nothing to clean)
        stats2 = await manager.run_cleanup_cycle()
        assert stats2.images_deleted == 0


class TestErrorHandling:
    """Test error handling scenarios."""

    @pytest.fixture
    def mock_database(self):
        """Create a mock database module."""
        with patch("app.core.cleanup_manager.database") as mock_db:
            mock_db.get_system_setting.return_value = None
            mock_db.get_all_projects.return_value = []
            yield mock_db

    def test_load_config_handles_all_exceptions(self, mock_database):
        """Should handle any exception during config loading."""
        mock_database.get_system_setting.side_effect = RuntimeError("Unexpected error")

        manager = CleanupManager()

        # Should not raise
        manager._load_config()

        # Manager should still work
        assert True

    def test_scan_handles_permission_errors(self, temp_dir):
        """Should handle permission errors when scanning files."""
        manager = CleanupManager()
        folder = temp_dir / "protected"
        folder.mkdir()

        # Mock iterdir to raise permission error
        with patch.object(Path, "iterdir", side_effect=PermissionError()):
            files = manager._scan_old_files(folder, max_age_days=7)

        assert files == []

    def test_delete_handles_file_not_found(self, temp_dir):
        """Should handle file not found during deletion."""
        manager = CleanupManager()

        # Try to delete non-existent file
        files_info = [{"path": str(temp_dir / "ghost.txt"), "size": 100}]

        count, bytes_freed = manager._delete_files(files_info)

        assert count == 0
        assert bytes_freed == 0

    def test_get_config_with_none_cache(self):
        """Should handle None config_loaded_at."""
        with patch("app.core.cleanup_manager.database") as mock_db:
            mock_db.get_system_setting.return_value = None

            manager = CleanupManager()
            manager._config_loaded_at = None

            # Should trigger load
            result = manager.get_config("cleanup_interval_minutes")

            assert result == DEFAULT_CONFIG["cleanup_interval_minutes"]


class TestVideoAndUploadCleanup:
    """Test video and upload file cleanup specifically."""

    @pytest.fixture
    def mock_database(self):
        """Create a mock database module."""
        with patch("app.core.cleanup_manager.database") as mock_db:
            mock_db.get_system_setting.return_value = None
            mock_db.get_all_projects.return_value = []
            mock_db.cleanup_expired_sessions = MagicMock()
            mock_db.cleanup_expired_lockouts = MagicMock()
            mock_db.cleanup_expired_api_key_sessions = MagicMock()
            mock_db.cleanup_old_sync_logs = MagicMock()
            mock_db.cleanup_old_login_attempts = MagicMock()
            yield mock_db

    @pytest.fixture
    def mock_settings(self, temp_dir):
        """Mock settings with temp directory."""
        with patch("app.core.cleanup_manager.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir
            yield mock_settings

    def test_preview_includes_project_videos(self, mock_database, mock_settings, temp_dir):
        """Should include videos from projects in preview."""
        # Set up project
        project_path = temp_dir / "video-project"
        project_path.mkdir()
        videos_folder = project_path / "generated-videos"
        videos_folder.mkdir()

        mock_database.get_all_projects.return_value = [
            {"id": "proj1", "name": "Video Project", "path": "video-project"}
        ]

        manager = CleanupManager()
        manager._config_cache = {
            "cleanup_images_enabled": False,
            "cleanup_videos_enabled": True,
            "cleanup_videos_max_age_days": 7,
            "cleanup_uploads_enabled": False,
            "cleanup_project_ids": [],
        }
        manager._config_loaded_at = datetime.utcnow()

        # Create old video in project
        old_video = videos_folder / "project_video.mp4"
        old_video.write_text("project video data")
        old_time = (datetime.now() - timedelta(days=10)).timestamp()
        os.utime(old_video, (old_time, old_time))

        preview = manager.preview_file_cleanup()

        # Should include project video
        project_videos = [f for f in preview.videos if f["project"] == "Video Project"]
        assert len(project_videos) == 1
        assert project_videos[0]["name"] == "project_video.mp4"

    def test_preview_includes_project_uploads(self, mock_database, mock_settings, temp_dir):
        """Should include uploads from projects in preview."""
        # Set up project
        project_path = temp_dir / "upload-project"
        project_path.mkdir()
        uploads_folder = project_path / "uploads"
        uploads_folder.mkdir()

        mock_database.get_all_projects.return_value = [
            {"id": "proj1", "name": "Upload Project", "path": "upload-project"}
        ]

        manager = CleanupManager()
        manager._config_cache = {
            "cleanup_images_enabled": False,
            "cleanup_videos_enabled": False,
            "cleanup_uploads_enabled": True,
            "cleanup_uploads_max_age_days": 7,
            "cleanup_project_ids": [],
        }
        manager._config_loaded_at = datetime.utcnow()

        # Create old upload in project
        old_upload = uploads_folder / "project_upload.zip"
        old_upload.write_text("project upload data")
        old_time = (datetime.now() - timedelta(days=10)).timestamp()
        os.utime(old_upload, (old_time, old_time))

        preview = manager.preview_file_cleanup()

        # Should include project upload
        project_uploads = [f for f in preview.uploads if f["project"] == "Upload Project"]
        assert len(project_uploads) == 1
        assert project_uploads[0]["name"] == "project_upload.zip"

    @pytest.mark.asyncio
    async def test_run_file_cleanup_deletes_videos(self, mock_database, mock_settings, temp_dir):
        """Should delete old video files and return stats."""
        manager = CleanupManager()
        manager._config_cache = {
            "cleanup_images_enabled": False,
            "cleanup_videos_enabled": True,
            "cleanup_videos_max_age_days": 7,
            "cleanup_uploads_enabled": False,
            "cleanup_project_ids": [],
        }
        manager._config_loaded_at = datetime.utcnow()

        # Create folder with old video
        videos_folder = temp_dir / "generated-videos"
        videos_folder.mkdir()
        old_video = videos_folder / "old.mp4"
        old_video.write_text("video data here")
        old_time = (datetime.now() - timedelta(days=10)).timestamp()
        os.utime(old_video, (old_time, old_time))

        stats = await manager.run_file_cleanup()

        assert stats.videos_deleted == 1
        assert stats.bytes_freed > 0
        assert not old_video.exists()

    @pytest.mark.asyncio
    async def test_run_file_cleanup_deletes_uploads(self, mock_database, mock_settings, temp_dir):
        """Should delete old upload files and return stats."""
        manager = CleanupManager()
        manager._config_cache = {
            "cleanup_images_enabled": False,
            "cleanup_videos_enabled": False,
            "cleanup_uploads_enabled": True,
            "cleanup_uploads_max_age_days": 7,
            "cleanup_project_ids": [],
        }
        manager._config_loaded_at = datetime.utcnow()

        # Create folder with old upload
        uploads_folder = temp_dir / "uploads"
        uploads_folder.mkdir()
        old_upload = uploads_folder / "old.zip"
        old_upload.write_text("upload data here")
        old_time = (datetime.now() - timedelta(days=10)).timestamp()
        os.utime(old_upload, (old_time, old_time))

        stats = await manager.run_file_cleanup()

        assert stats.uploads_deleted == 1
        assert stats.bytes_freed > 0
        assert not old_upload.exists()

    @pytest.mark.asyncio
    async def test_run_file_cleanup_deletes_all_types(self, mock_database, mock_settings, temp_dir):
        """Should delete all file types when all cleanup is enabled."""
        manager = CleanupManager()
        manager._config_cache = {
            "cleanup_images_enabled": True,
            "cleanup_images_max_age_days": 7,
            "cleanup_videos_enabled": True,
            "cleanup_videos_max_age_days": 7,
            "cleanup_uploads_enabled": True,
            "cleanup_uploads_max_age_days": 7,
            "cleanup_project_ids": [],
        }
        manager._config_loaded_at = datetime.utcnow()

        # Create folders with old files
        old_time = (datetime.now() - timedelta(days=10)).timestamp()

        images_folder = temp_dir / "generated-images"
        images_folder.mkdir()
        old_image = images_folder / "old.png"
        old_image.write_text("image data")
        os.utime(old_image, (old_time, old_time))

        videos_folder = temp_dir / "generated-videos"
        videos_folder.mkdir()
        old_video = videos_folder / "old.mp4"
        old_video.write_text("video data")
        os.utime(old_video, (old_time, old_time))

        uploads_folder = temp_dir / "uploads"
        uploads_folder.mkdir()
        old_upload = uploads_folder / "old.zip"
        old_upload.write_text("upload data")
        os.utime(old_upload, (old_time, old_time))

        stats = await manager.run_file_cleanup()

        assert stats.images_deleted == 1
        assert stats.videos_deleted == 1
        assert stats.uploads_deleted == 1
        assert stats.bytes_freed > 0
        assert not old_image.exists()
        assert not old_video.exists()
        assert not old_upload.exists()


class TestConfigParsingEdgeCases:
    """Test edge cases in config parsing."""

    @pytest.fixture
    def mock_database(self):
        """Create a mock database module."""
        with patch("app.core.cleanup_manager.database") as mock_db:
            yield mock_db

    def test_load_config_parses_yes_as_true(self, mock_database):
        """Should parse 'yes' string as boolean True."""
        def get_setting(key):
            if key == "cleanup_sleep_mode_enabled":
                return "yes"
            return None

        mock_database.get_system_setting.side_effect = get_setting

        manager = CleanupManager()
        manager._load_config()

        assert manager._config_cache["sleep_mode_enabled"] is True

    def test_load_config_parses_1_as_true(self, mock_database):
        """Should parse '1' string as boolean True."""
        def get_setting(key):
            if key == "cleanup_sleep_mode_enabled":
                return "1"
            return None

        mock_database.get_system_setting.side_effect = get_setting

        manager = CleanupManager()
        manager._load_config()

        assert manager._config_cache["sleep_mode_enabled"] is True

    def test_load_config_parses_other_as_false(self, mock_database):
        """Should parse other strings as boolean False."""
        def get_setting(key):
            if key == "cleanup_sleep_mode_enabled":
                return "no"
            return None

        mock_database.get_system_setting.side_effect = get_setting

        manager = CleanupManager()
        manager._load_config()

        assert manager._config_cache["sleep_mode_enabled"] is False
