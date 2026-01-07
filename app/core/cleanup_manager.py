"""
Cleanup Manager - Centralized background cleanup and sleep mode management

Handles:
- Configurable cleanup schedules for sessions, connections, and database records
- Generated file cleanup (images, videos, uploads) per project
- Sleep mode to reduce resource usage when idle
"""

import logging
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, List, Any
from dataclasses import dataclass, field

from app.db import database
from app.core.config import settings

logger = logging.getLogger(__name__)


# Default configuration values
DEFAULT_CONFIG = {
    # Cleanup schedule
    "cleanup_interval_minutes": 5,
    "sdk_session_max_age_minutes": 60,
    "websocket_max_age_minutes": 5,
    "sync_log_retention_hours": 24,

    # File cleanup (disabled by default)
    "cleanup_images_enabled": False,
    "cleanup_images_max_age_days": 7,
    "cleanup_videos_enabled": False,
    "cleanup_videos_max_age_days": 7,
    "cleanup_uploads_enabled": False,
    "cleanup_uploads_max_age_days": 7,
    "cleanup_project_ids": [],  # List of project IDs, empty = all projects

    # Sleep mode
    "sleep_mode_enabled": True,
    "sleep_timeout_minutes": 10,
}


@dataclass
class CleanupStats:
    """Statistics from a cleanup run"""
    sdk_sessions_cleaned: int = 0
    websocket_connections_cleaned: int = 0
    auth_sessions_cleaned: int = 0
    sync_logs_cleaned: int = 0
    images_deleted: int = 0
    videos_deleted: int = 0
    uploads_deleted: int = 0
    bytes_freed: int = 0


@dataclass
class FileCleanupPreview:
    """Preview of files that would be deleted"""
    images: List[Dict[str, Any]] = field(default_factory=list)
    videos: List[Dict[str, Any]] = field(default_factory=list)
    uploads: List[Dict[str, Any]] = field(default_factory=list)
    total_count: int = 0
    total_bytes: int = 0


class CleanupManager:
    """
    Manages background cleanup tasks and sleep mode.

    Configuration is stored in system_settings table with 'cleanup_' prefix.
    """

    def __init__(self):
        self._last_activity: datetime = datetime.utcnow()
        self._is_sleeping: bool = False
        self._config_cache: Dict[str, Any] = {}
        self._config_loaded_at: Optional[datetime] = None
        self._cleanup_callbacks: List[callable] = []

    def record_activity(self):
        """Record user activity to prevent sleep mode"""
        self._last_activity = datetime.utcnow()
        if self._is_sleeping:
            self._wake_up()

    def _wake_up(self):
        """Wake up from sleep mode"""
        if self._is_sleeping:
            logger.info("Waking up from sleep mode due to user activity")
            self._is_sleeping = False

    def is_sleeping(self) -> bool:
        """Check if the app is in sleep mode"""
        return self._is_sleeping

    def get_idle_seconds(self) -> float:
        """Get seconds since last activity"""
        return (datetime.utcnow() - self._last_activity).total_seconds()

    def get_config(self, key: str, default: Any = None) -> Any:
        """Get a cleanup configuration value"""
        # Refresh cache every 60 seconds
        if (self._config_loaded_at is None or
            (datetime.utcnow() - self._config_loaded_at).total_seconds() > 60):
            self._load_config()

        return self._config_cache.get(key, default if default is not None else DEFAULT_CONFIG.get(key))

    def _load_config(self):
        """Load configuration from database"""
        try:
            for key, default in DEFAULT_CONFIG.items():
                db_key = f"cleanup_{key}"
                value = database.get_system_setting(db_key)
                if value is not None:
                    # Parse JSON for list/dict types
                    if isinstance(default, (list, dict)):
                        try:
                            self._config_cache[key] = json.loads(value)
                        except json.JSONDecodeError:
                            self._config_cache[key] = default
                    elif isinstance(default, bool):
                        self._config_cache[key] = value.lower() in ('true', '1', 'yes')
                    elif isinstance(default, int):
                        self._config_cache[key] = int(value)
                    else:
                        self._config_cache[key] = value
                else:
                    self._config_cache[key] = default
            self._config_loaded_at = datetime.utcnow()
        except Exception as e:
            logger.error(f"Error loading cleanup config: {e}")

    def set_config(self, key: str, value: Any):
        """Set a cleanup configuration value"""
        db_key = f"cleanup_{key}"

        # Convert to string for storage
        if isinstance(value, bool):
            str_value = "true" if value else "false"
        elif isinstance(value, (list, dict)):
            str_value = json.dumps(value)
        else:
            str_value = str(value)

        database.set_system_setting(db_key, str_value)
        self._config_cache[key] = value

    def get_all_config(self) -> Dict[str, Any]:
        """Get all cleanup configuration"""
        self._load_config()
        return {**DEFAULT_CONFIG, **self._config_cache}

    def should_sleep(self) -> bool:
        """Check if the app should enter sleep mode"""
        if not self.get_config("sleep_mode_enabled"):
            return False

        timeout_minutes = self.get_config("sleep_timeout_minutes")
        idle_minutes = self.get_idle_seconds() / 60

        return idle_minutes >= timeout_minutes

    async def check_sleep_mode(self):
        """Check and enter sleep mode if appropriate"""
        if self.should_sleep() and not self._is_sleeping:
            logger.info(f"Entering sleep mode after {self.get_config('sleep_timeout_minutes')} minutes of inactivity")
            self._is_sleeping = True

    def get_projects_to_clean(self) -> List[Dict[str, Any]]:
        """Get list of projects to clean based on configuration"""
        selected_ids = self.get_config("cleanup_project_ids")

        if not selected_ids:
            # Return all projects
            return database.get_all_projects()

        # Return only selected projects
        all_projects = database.get_all_projects()
        return [p for p in all_projects if p["id"] in selected_ids]

    def _get_generated_folders(self, project_path: Path) -> Dict[str, Path]:
        """Get generated folder paths for a project"""
        return {
            "images": project_path / "generated-images",
            "videos": project_path / "generated-videos",
            "uploads": project_path / "uploads"
        }

    def _scan_old_files(self, folder: Path, max_age_days: int) -> List[Dict[str, Any]]:
        """Scan for files older than max_age_days"""
        old_files = []
        if not folder.exists():
            return old_files

        # Use local time consistently for both cutoff and file mtime
        now = datetime.now()
        cutoff = now - timedelta(days=max_age_days)

        try:
            for file_path in folder.iterdir():
                if file_path.is_file():
                    mtime = datetime.fromtimestamp(file_path.stat().st_mtime)
                    if mtime < cutoff:
                        age_days = (now - mtime).days
                        old_files.append({
                            "path": str(file_path),
                            "name": file_path.name,
                            "size": file_path.stat().st_size,
                            "modified": mtime.isoformat(),
                            "age_days": age_days
                        })
        except Exception as e:
            logger.error(f"Error scanning folder {folder}: {e}")

        return old_files

    def preview_file_cleanup(self) -> FileCleanupPreview:
        """Preview files that would be deleted without actually deleting"""
        preview = FileCleanupPreview()

        projects = self.get_projects_to_clean()

        for project in projects:
            # Project paths are relative to workspace - join with workspace dir
            project_path = settings.effective_workspace_dir / project["path"]
            folders = self._get_generated_folders(project_path)

            # Images
            if self.get_config("cleanup_images_enabled"):
                max_age = self.get_config("cleanup_images_max_age_days")
                files = self._scan_old_files(folders["images"], max_age)
                for f in files:
                    f["project"] = project["name"]
                preview.images.extend(files)

            # Videos
            if self.get_config("cleanup_videos_enabled"):
                max_age = self.get_config("cleanup_videos_max_age_days")
                files = self._scan_old_files(folders["videos"], max_age)
                for f in files:
                    f["project"] = project["name"]
                preview.videos.extend(files)

            # Uploads
            if self.get_config("cleanup_uploads_enabled"):
                max_age = self.get_config("cleanup_uploads_max_age_days")
                files = self._scan_old_files(folders["uploads"], max_age)
                for f in files:
                    f["project"] = project["name"]
                preview.uploads.extend(files)

        # Also check workspace root folders
        workspace = settings.effective_workspace_dir
        root_folders = self._get_generated_folders(workspace)

        if self.get_config("cleanup_images_enabled"):
            max_age = self.get_config("cleanup_images_max_age_days")
            files = self._scan_old_files(root_folders["images"], max_age)
            for f in files:
                f["project"] = "(workspace root)"
            preview.images.extend(files)

        if self.get_config("cleanup_videos_enabled"):
            max_age = self.get_config("cleanup_videos_max_age_days")
            files = self._scan_old_files(root_folders["videos"], max_age)
            for f in files:
                f["project"] = "(workspace root)"
            preview.videos.extend(files)

        if self.get_config("cleanup_uploads_enabled"):
            max_age = self.get_config("cleanup_uploads_max_age_days")
            files = self._scan_old_files(root_folders["uploads"], max_age)
            for f in files:
                f["project"] = "(workspace root)"
            preview.uploads.extend(files)

        # Calculate totals
        preview.total_count = len(preview.images) + len(preview.videos) + len(preview.uploads)
        preview.total_bytes = (
            sum(f["size"] for f in preview.images) +
            sum(f["size"] for f in preview.videos) +
            sum(f["size"] for f in preview.uploads)
        )

        return preview

    def _delete_files(self, files: List[Dict[str, Any]]) -> tuple[int, int]:
        """Delete files and return (count, bytes_freed)"""
        deleted_count = 0
        bytes_freed = 0

        for file_info in files:
            try:
                file_path = Path(file_info["path"])
                if file_path.exists():
                    size = file_path.stat().st_size
                    file_path.unlink()
                    deleted_count += 1
                    bytes_freed += size
                    logger.debug(f"Deleted: {file_path}")
            except Exception as e:
                logger.error(f"Error deleting {file_info['path']}: {e}")

        return deleted_count, bytes_freed

    async def run_file_cleanup(self) -> CleanupStats:
        """Run file cleanup based on current configuration"""
        stats = CleanupStats()
        preview = self.preview_file_cleanup()

        # Delete images
        if preview.images:
            count, bytes_freed = self._delete_files(preview.images)
            stats.images_deleted = count
            stats.bytes_freed += bytes_freed

        # Delete videos
        if preview.videos:
            count, bytes_freed = self._delete_files(preview.videos)
            stats.videos_deleted = count
            stats.bytes_freed += bytes_freed

        # Delete uploads
        if preview.uploads:
            count, bytes_freed = self._delete_files(preview.uploads)
            stats.uploads_deleted = count
            stats.bytes_freed += bytes_freed

        if stats.images_deleted or stats.videos_deleted or stats.uploads_deleted:
            logger.info(
                f"File cleanup: deleted {stats.images_deleted} images, "
                f"{stats.videos_deleted} videos, {stats.uploads_deleted} uploads "
                f"({stats.bytes_freed / 1024 / 1024:.2f} MB freed)"
            )

        return stats

    async def run_cleanup_cycle(
        self,
        cleanup_sessions_callback: Optional[callable] = None,
        cleanup_connections_callback: Optional[callable] = None
    ) -> CleanupStats:
        """
        Run a full cleanup cycle.

        Args:
            cleanup_sessions_callback: Async function to cleanup SDK sessions (takes max_age_seconds)
            cleanup_connections_callback: Async function to cleanup WebSocket connections (takes max_age_seconds)
        """
        stats = CleanupStats()

        # Check sleep mode first
        await self.check_sleep_mode()

        if self._is_sleeping:
            logger.debug("Skipping cleanup cycle - app is sleeping")
            return stats

        logger.debug("Running cleanup cycle...")

        # SDK sessions cleanup
        if cleanup_sessions_callback:
            max_age = self.get_config("sdk_session_max_age_minutes") * 60
            await cleanup_sessions_callback(max_age_seconds=max_age)

        # WebSocket connections cleanup
        if cleanup_connections_callback:
            max_age = self.get_config("websocket_max_age_minutes") * 60
            await cleanup_connections_callback(max_age_seconds=max_age)

        # Database cleanup (auth sessions, lockouts, API key sessions)
        database.cleanup_expired_sessions()
        database.cleanup_expired_lockouts()
        database.cleanup_expired_api_key_sessions()

        # Sync logs cleanup
        retention_hours = self.get_config("sync_log_retention_hours")
        database.cleanup_old_sync_logs(max_age_hours=retention_hours)
        database.cleanup_old_login_attempts(max_age_hours=retention_hours)

        # File cleanup (if enabled)
        file_stats = await self.run_file_cleanup()
        stats.images_deleted = file_stats.images_deleted
        stats.videos_deleted = file_stats.videos_deleted
        stats.uploads_deleted = file_stats.uploads_deleted
        stats.bytes_freed = file_stats.bytes_freed

        logger.debug("Cleanup cycle completed")
        return stats

    def get_status(self) -> Dict[str, Any]:
        """Get current cleanup manager status"""
        return {
            "is_sleeping": self._is_sleeping,
            "idle_seconds": self.get_idle_seconds(),
            "last_activity": self._last_activity.isoformat(),
            "sleep_mode_enabled": self.get_config("sleep_mode_enabled"),
            "sleep_timeout_minutes": self.get_config("sleep_timeout_minutes"),
            "cleanup_interval_minutes": self.get_config("cleanup_interval_minutes"),
        }


# Global cleanup manager instance
cleanup_manager = CleanupManager()
