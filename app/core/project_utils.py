"""
Project path resolution utilities.

Single source of truth for resolving project and worktree filesystem paths.
Supports both absolute paths (local mode) and workspace-relative paths (Docker mode).

Convention:
- Absolute paths: start with '/' (Unix) or contain ':\\' or ':/' (Windows)
- Relative paths: resolved against settings.workspace_dir
"""

import sys
from pathlib import Path
from typing import Union

from app.core.config import settings


def is_absolute_project_path(path: str) -> bool:
    """
    Check if a project path is absolute (local mode) vs relative (workspace-relative).

    Convention:
    - Unix absolute: starts with '/'
    - Windows absolute: contains ':\\' or ':/' (e.g., 'D:\\repos' or 'D:/repos')
    - Everything else: relative to workspace_dir
    """
    if not path:
        return False
    # Unix absolute
    if path.startswith("/"):
        return True
    # Windows absolute (e.g., "C:\\", "D:/")
    if len(path) >= 3 and path[1] == ":" and path[2] in ("/", "\\"):
        return True
    return False


def get_project_working_dir(project: Union[dict, str]) -> str:
    """
    Resolve the filesystem working directory for a project.

    Args:
        project: Either a project dict (with 'path' key) or a path string directly.

    Returns:
        Absolute filesystem path as string.
    """
    path = project["path"] if isinstance(project, dict) else project

    if is_absolute_project_path(path):
        return path

    return str(settings.workspace_dir / path)


def get_project_working_dir_as_path(project: Union[dict, str]) -> Path:
    """
    Resolve the filesystem working directory for a project as a Path object.

    Args:
        project: Either a project dict (with 'path' key) or a path string directly.

    Returns:
        Absolute filesystem path as Path.
    """
    return Path(get_project_working_dir(project))


def get_worktree_working_dir(worktree: dict) -> str:
    """
    Resolve the filesystem working directory for a worktree.

    Worktree paths in the database are stored as folder names (relative).
    They are resolved against workspace_dir for workspace-relative projects.

    For absolute-path projects, worktrees are sibling directories,
    so their stored path is just the folder name relative to the parent.

    Args:
        worktree: Worktree dict with 'worktree_path' key.

    Returns:
        Absolute filesystem path as string.

    Raises:
        ValueError: If worktree_path is None or empty.
    """
    worktree_path = worktree.get("worktree_path")

    if not worktree_path:
        wt_id = worktree.get("id", "unknown")
        raise ValueError(
            f"Worktree {wt_id} has no worktree_path. "
            f"This usually means the worktree record is corrupt or incomplete."
        )

    # If the stored path is already absolute, use it directly
    if is_absolute_project_path(worktree_path):
        return worktree_path

    # Otherwise resolve against workspace_dir (current behavior)
    return str(settings.workspace_dir / worktree_path)


def get_worktree_working_dir_as_path(worktree: dict) -> Path:
    """Resolve worktree working directory as a Path object."""
    return Path(get_worktree_working_dir(worktree))


def get_project_parent_dir(project: Union[dict, str]) -> Path:
    """
    Get the parent directory of a project.

    Used for placing worktrees as siblings of the project directory.

    For workspace-relative projects: returns workspace_dir
    For absolute-path projects: returns the project's parent directory
    """
    path = project["path"] if isinstance(project, dict) else project

    if is_absolute_project_path(path):
        return Path(path).parent

    return settings.workspace_dir
