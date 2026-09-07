"""
Project Memory Service - Manages AGENTS.md memory files in project directories.

This service handles reading, creating, updating, and deleting memory files
at two locations within a project:

1. Project root: <project>/AGENTS.md  (shared, checked into version control)
2. Local/private: <project>/.agents/AGENTS.md  (local instructions, typically gitignored)

The older CLAUDE.md / .claude/CLAUDE.md files are still READ as a legacy
fallback when no AGENTS.md exists at that location, but they are never
written to: a save always creates the AGENTS.md file and leaves any legacy
file untouched.

These files provide persistent memory/instructions for agent sessions
working within the project.
"""

import logging
import threading
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any, List
from dataclasses import dataclass
from app.core.resource_sync import mutation

logger = logging.getLogger(__name__)


# Valid memory file locations - these are the ONLY paths ever written.
MEMORY_LOCATIONS = {
    "project": "AGENTS.md",           # Project root
    "local": ".agents/AGENTS.md",     # Local/private
}

# Legacy paths, read as a fallback when no primary file exists. Never written,
# never created, and only deleted when no primary file is present.
LEGACY_MEMORY_LOCATIONS = {
    "project": "CLAUDE.md",
    "local": ".claude/CLAUDE.md",
}


@dataclass
class MemoryFile:
    """Information about an AGENTS.md memory file"""
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
    Service for managing AGENTS.md memory files in project directories.

    Memory files are plain markdown files that provide persistent context
    and instructions for agent sessions. Each location has a primary path
    (AGENTS.md / .agents/AGENTS.md) and a read-only legacy path
    (CLAUDE.md / .claude/CLAUDE.md) that is used when no primary file
    exists yet.
    """

    def __init__(self, project_path: Path):
        self.project_path = project_path

    def _validate_location(self, location: str) -> None:
        """Raise ValueError unless ``location`` is a known memory location."""
        if location not in MEMORY_LOCATIONS:
            raise ValueError(
                f"Invalid memory location: '{location}'. "
                f"Must be one of: {', '.join(MEMORY_LOCATIONS.keys())}"
            )

    def _get_file_path(self, location: str) -> Path:
        """
        Get the absolute path of the PRIMARY memory file for a location.

        This is the path that a save always writes to, whether or not a
        legacy file exists.

        Args:
            location: "project" or "local"

        Returns:
            Absolute Path to the primary memory file

        Raises:
            ValueError: If location is not valid
        """
        self._validate_location(location)
        return self.project_path / MEMORY_LOCATIONS[location]

    def _get_legacy_file_path(self, location: str) -> Path:
        """
        Get the absolute path of the LEGACY memory file for a location.

        Args:
            location: "project" or "local"

        Returns:
            Absolute Path to the legacy memory file

        Raises:
            ValueError: If location is not valid
        """
        self._validate_location(location)
        return self.project_path / LEGACY_MEMORY_LOCATIONS[location]

    def _resolve(self, location: str) -> tuple:
        """
        Resolve a location to the file that should be read or deleted.

        Prefers the primary file when it exists on disk, falls back to the
        legacy file when that exists, and otherwise reports the primary
        path (which does not exist yet).

        Args:
            location: "project" or "local"

        Returns:
            (absolute Path, relative path string)
        """
        primary = self._get_file_path(location)
        if primary.exists():
            return primary, MEMORY_LOCATIONS[location]

        legacy = self._get_legacy_file_path(location)
        if legacy.exists():
            return legacy, LEGACY_MEMORY_LOCATIONS[location]

        return primary, MEMORY_LOCATIONS[location]

    def _read_file(self, location: str, file_path: Path, rel_path: str) -> MemoryFile:
        """Read a memory file and return its info."""
        if not file_path.exists():
            return MemoryFile(
                location=location,
                exists=False,
                content="",
                file_path=rel_path,
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
                file_path=rel_path,
                size=stat.st_size,
                updated_at=datetime.fromtimestamp(stat.st_mtime),
            )
        except (IOError, UnicodeDecodeError) as e:
            logger.error(f"Failed to read memory file {file_path}: {e}")
            return MemoryFile(
                location=location,
                exists=True,
                content="",
                file_path=rel_path,
                size=0,
                updated_at=None,
            )

    def get_all(self) -> List[MemoryFile]:
        """
        Get info about all memory file locations.

        Returns list of MemoryFile objects for each location,
        including those that don't exist yet.
        """
        return [self.get(location) for location in MEMORY_LOCATIONS]

    def get(self, location: str) -> MemoryFile:
        """
        Get a specific memory file.

        Reads the primary file when present, otherwise the legacy file.
        ``file_path`` reports whichever one was resolved.

        Args:
            location: "project" or "local"

        Returns:
            MemoryFile with content and metadata
        """
        file_path, rel_path = self._resolve(location)
        return self._read_file(location, file_path, rel_path)

    @mutation("projects", scope="shared", database=False)
    def save(self, location: str, content: str) -> MemoryFile:
        """
        Create or update a memory file.

        Always writes the PRIMARY path (AGENTS.md / .agents/AGENTS.md),
        creating the parent directory if needed. Any legacy file at the
        same location is left untouched.

        Args:
            location: "project" or "local"
            content: File content (markdown text)

        Returns:
            Updated MemoryFile (pointing at the primary path)
        """
        file_path = self._get_file_path(location)

        # Ensure parent directory exists (e.g. .agents/ for the local scope)
        file_path.parent.mkdir(parents=True, exist_ok=True)

        # Write content
        file_path.write_text(content, encoding="utf-8")

        return self._read_file(location, file_path, MEMORY_LOCATIONS[location])

    @mutation("projects", scope="shared", database=False)
    def delete(self, location: str) -> bool:
        """
        Delete a memory file.

        Deletes the resolved file only - the primary file when it exists,
        otherwise the legacy file. Never both.

        Args:
            location: "project" or "local"

        Returns:
            True if file was deleted

        Raises:
            ValueError: If file doesn't exist
        """
        file_path, _ = self._resolve(location)

        if not file_path.exists():
            raise ValueError(
                f"Memory file does not exist at {MEMORY_LOCATIONS[location]} "
                f"(or legacy {LEGACY_MEMORY_LOCATIONS[location]})"
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
