"""
Application configuration and settings

Supports both Docker and Local deployment modes:
- Docker: Uses /data, /workspace, /home/appuser paths
- Local: Uses platform-specific paths (AppData, ~/.local/share, etc.)

Set LOCAL_MODE=true environment variable to force local mode.
"""

import os
from pathlib import Path
from typing import Optional
from pydantic_settings import BaseSettings

# Import platform detection (lazy to avoid circular imports)
def _get_default_data_dir() -> Path:
    """Get default data directory based on deployment mode"""
    from app.core.platform import get_app_data_dir
    return get_app_data_dir()

def _get_default_workspace_dir() -> Path:
    """Get default workspace directory based on deployment mode"""
    from app.core.platform import get_default_workspace_dir
    return get_default_workspace_dir()


class Settings(BaseSettings):
    """Application settings loaded from environment variables"""

    # Server
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"

    # Service info
    service_name: str = "ai-shuffle"
    version: str = "4.0.0"

    # Deployment mode (auto-detected if not set)
    local_mode: Optional[bool] = None  # None = auto-detect, True = local, False = docker

    # Paths - these will be set dynamically if not provided
    data_dir: Optional[Path] = None
    workspace_dir: Optional[Path] = None
    claude_projects_dir: Optional[Path] = None  # Override for Claude SDK projects dir (defaults to ~/.claude/projects)
    tools_dir: Optional[Path] = None  # Path to AI tools directory (defaults to /workspace/ai-shuffle/tools in Docker)

    # Database
    database_url: Optional[str] = None

    # Session
    session_secret: Optional[str] = None
    session_expire_days: int = 30

    # Claude
    command_timeout: int = 300

    # Security - Rate Limiting
    max_login_attempts: int = 5  # Max failed attempts before lockout
    login_attempt_window_minutes: int = 15  # Time window for counting attempts
    lockout_duration_minutes: int = 30  # Duration of lockout after max attempts

    # Security - API Key Session
    api_key_session_expire_hours: int = 24  # API key web session duration

    # Security - Trusted Proxies (for getting real IP behind reverse proxy)
    trusted_proxy_headers: str = "X-Forwarded-For,X-Real-IP"  # Comma-separated

    # Security - CORS (comma-separated origins, or "*" for development only)
    cors_origins: str = "http://localhost:8000,http://127.0.0.1:8000"

    # Security - Cookie settings
    cookie_secure: bool = True  # Set to False only for local HTTP development

    # Security - Request limits
    max_request_body_mb: int = 50  # Maximum request body size in MB

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

    def model_post_init(self, __context) -> None:
        """Initialize paths based on deployment mode after model creation"""
        # Set data_dir if not provided
        if self.data_dir is None:
            object.__setattr__(self, 'data_dir', _get_default_data_dir())

        # Set workspace_dir if not provided
        if self.workspace_dir is None:
            object.__setattr__(self, 'workspace_dir', _get_default_workspace_dir())

    @property
    def effective_data_dir(self) -> Path:
        """Get the effective data directory (always returns a valid Path)"""
        if self.data_dir is not None:
            return self.data_dir
        return _get_default_data_dir()

    @property
    def effective_workspace_dir(self) -> Path:
        """Get the effective workspace directory (always returns a valid Path)"""
        if self.workspace_dir is not None:
            return self.workspace_dir
        return _get_default_workspace_dir()

    @property
    def db_path(self) -> Path:
        """Get the SQLite database path"""
        return self.effective_data_dir / "db.sqlite"

    @property
    def sessions_dir(self) -> Path:
        """Get the sessions directory"""
        return self.effective_data_dir / "sessions"

    @property
    def get_claude_projects_dir(self) -> Path:
        """Get the Claude SDK projects directory"""
        if self.claude_projects_dir:
            return self.claude_projects_dir
        return Path.home() / ".claude" / "projects"

    @property
    def effective_tools_dir(self) -> Path:
        """Get the effective AI tools directory"""
        if self.tools_dir is not None:
            return self.tools_dir
        # Default to /opt/ai-tools in Docker mode (bundled in image, accessible by Claude agent)
        if not self.is_local_mode():
            return Path("/opt/ai-tools")
        # In local mode, tools would be relative to install location
        return Path(__file__).parent.parent.parent / "tools"

    def get_database_url(self) -> str:
        """Get database URL, defaulting to SQLite"""
        if self.database_url:
            return self.database_url
        return f"sqlite:///{self.db_path}"

    def is_local_mode(self) -> bool:
        """Check if running in local mode"""
        if self.local_mode is not None:
            return self.local_mode
        from app.core.platform import is_local_mode as platform_is_local
        return platform_is_local()

    def get_deployment_info(self) -> dict:
        """Get deployment configuration for diagnostics"""
        from app.core.platform import get_platform_info
        return {
            **get_platform_info(),
            "data_dir": str(self.effective_data_dir),
            "workspace_dir": str(self.effective_workspace_dir),
            "db_path": str(self.db_path),
            "sessions_dir": str(self.sessions_dir),
        }


# Global settings instance
settings = Settings()


# Ensure directories exist
def ensure_directories():
    """Create required directories if they don't exist"""
    settings.effective_data_dir.mkdir(parents=True, exist_ok=True)
    settings.sessions_dir.mkdir(parents=True, exist_ok=True)
    settings.effective_workspace_dir.mkdir(parents=True, exist_ok=True)


def load_workspace_from_database():
    """
    Load workspace path from database if configured.

    This should be called after database initialization to apply any
    user-configured workspace path from local mode setup.
    """
    import logging
    logger = logging.getLogger(__name__)

    try:
        from app.db import database
        configured_path = database.get_system_setting("workspace_path")
        logger.info(f"Loading workspace from database: configured_path={configured_path}")

        if configured_path:
            path = Path(configured_path)
            if path.exists() or path.parent.exists():
                object.__setattr__(settings, 'workspace_dir', path)
                logger.info(f"Workspace directory set to: {path}")
            else:
                logger.warning(f"Configured workspace path does not exist: {path}")
        else:
            logger.info(f"No workspace path configured in database, using default: {settings.workspace_dir}")
    except Exception as e:
        logger.error(f"Failed to load workspace from database: {e}")
        # Database might not be initialized yet
        pass
