"""
Application configuration and settings
"""

import os
from pathlib import Path
from typing import Optional
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables"""

    # Server
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"

    # Service info
    service_name: str = "ai-hub"
    version: str = "4.0.0"

    # Paths
    data_dir: Path = Path("/data")
    workspace_dir: Path = Path("/workspace")

    # Database
    database_url: Optional[str] = None

    # Session
    session_secret: Optional[str] = None
    session_expire_days: int = 30

    # Claude
    command_timeout: int = 300

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

    @property
    def db_path(self) -> Path:
        """Get the SQLite database path"""
        return self.data_dir / "db.sqlite"

    @property
    def sessions_dir(self) -> Path:
        """Get the sessions directory"""
        return self.data_dir / "sessions"

    def get_database_url(self) -> str:
        """Get database URL, defaulting to SQLite"""
        if self.database_url:
            return self.database_url
        return f"sqlite:///{self.db_path}"


# Global settings instance
settings = Settings()


# Ensure directories exist
def ensure_directories():
    """Create required directories if they don't exist"""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.sessions_dir.mkdir(parents=True, exist_ok=True)
    settings.workspace_dir.mkdir(parents=True, exist_ok=True)
