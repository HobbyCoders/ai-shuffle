"""
Project Memory Service - Manages CLAUDE.md memory files in project directories.

This service handles reading, creating, updating, and deleting CLAUDE.md files
at two locations within a project:

1. Project root: <project>/CLAUDE.md  (shared, checked into version control)
2. Local/private: <project>/.claude/CLAUDE.md  (local overrides, typically gitignored)

These files provide persistent memory/instructions for Claude Code sessions
working within the project.
"""

import logging
import threading
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any, List
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# Valid memory file locations
MEMORY_LOCATIONS = {
    "project": "CLAUDE.md",           # Project root
    "local": ".claude/CLAUDE.md",     # Local/private
}


@dataclass
class MemoryFile:
    """Information about a CLAUDE.md memory file"""
    location: str       # "project" or "local"
    exists: bool
    content: str = ""
    file_path: str = ""  # Relative path within project
    size: int = 0
    updated_at: Optional[datetime] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "location": self.location,
            "exists": self.exists,
            "content": self.content,
            "file_path": self.file_path,
            "size": self.size,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class ProjectMemoryService:
    """
    Service for managing CLAUDE.md memory files in project directories.

    Memory files are plain markdown files that provide persistent context
    and instructions for Claude Code sessions.
    """

    def __init__(self, project_path: Path):
        self.project_path = project_path

    def _get_file_path(self, location: str) -> Path:
        """
        Get the absolute file path for a memory location.

        Args:
            location: "project" or "local"

        Returns:
            Absolute Path to the memory file

        Raises:
            ValueError: If location is not valid
        """
        if location not in MEMORY_LOCATIONS:
            raise ValueError(
                f"Invalid memory location: '{location}'. "
                f"Must be one of: {', '.join(MEMORY_LOCATIONS.keys())}"
            )
        return self.project_path / MEMORY_LOCATIONS[location]

    def _read_file(self, file_path: Path) -> MemoryFile:
        """Read a memory file and return its info."""
        location = None
        for loc, rel_path in MEMORY_LOCATIONS.items():
            if file_path == self.project_path / rel_path:
                location = loc
                break

        if location is None:
            raise ValueError(f"Unknown memory file path: {file_path}")

        if not file_path.exists():
            return MemoryFile(
                location=location,
                exists=False,
                content="",
                file_path=MEMORY_LOCATIONS[location],
                size=0,
                updated_at=None,
            )

        try:
            content = file_path.read_text(encoding="utf-8")
            stat = file_path.stat()
            return MemoryFile(
                location=location,
                exists=True,
                content=content,
                file_path=MEMORY_LOCATIONS[location],
                size=stat.st_size,
                updated_at=datetime.fromtimestamp(stat.st_mtime),
            )
        except (IOError, UnicodeDecodeError) as e:
            logger.error(f"Failed to read memory file {file_path}: {e}")
            return MemoryFile(
                location=location,
                exists=True,
                content="",
                file_path=MEMORY_LOCATIONS[location],
                size=0,
                updated_at=None,
            )

    def get_all(self) -> List[MemoryFile]:
        """
        Get info about all memory file locations.

        Returns list of MemoryFile objects for each location,
        including those that don't exist yet.
        """
        results = []
        for location in MEMORY_LOCATIONS:
            file_path = self._get_file_path(location)
            results.append(self._read_file(file_path))
        return results

    def get(self, location: str) -> MemoryFile:
        """
        Get a specific memory file.

        Args:
            location: "project" or "local"

        Returns:
            MemoryFile with content and metadata
        """
        file_path = self._get_file_path(location)
        return self._read_file(file_path)

    def save(self, location: str, content: str) -> MemoryFile:
        """
        Create or update a memory file.

        Args:
            location: "project" or "local"
            content: File content (markdown text)

        Returns:
            Updated MemoryFile
        """
        file_path = self._get_file_path(location)

        # Ensure parent directory exists
        file_path.parent.mkdir(parents=True, exist_ok=True)

        # Write content
        file_path.write_text(content, encoding="utf-8")

        return self._read_file(file_path)

    def delete(self, location: str) -> bool:
        """
        Delete a memory file.

        Args:
            location: "project" or "local"

        Returns:
            True if file was deleted

        Raises:
            ValueError: If file doesn't exist
        """
        file_path = self._get_file_path(location)

        if not file_path.exists():
            raise ValueError(
                f"Memory file does not exist at {MEMORY_LOCATIONS[location]}"
            )

        file_path.unlink()
        return True


# Service instance cache (keyed by project_path)
_memory_services: Dict[str, ProjectMemoryService] = {}
_memory_services_lock = threading.Lock()


def get_project_memory_service(project_path: Path) -> ProjectMemoryService:
    """
    Get a project memory service instance for a project directory.

    Args:
        project_path: Path to the project directory

    Returns:
        ProjectMemoryService instance (cached per project_path)
    """
    cache_key = str(project_path)

    with _memory_services_lock:
        if cache_key not in _memory_services:
            _memory_services[cache_key] = ProjectMemoryService(project_path)
        return _memory_services[cache_key]
