"""
Project path resolution utilities.

Single source of truth for resolving project and worktree filesystem paths.
Supports both absolute paths (local mode) and workspace-relative paths (Docker mode).

Convention:
- Absolute paths: start with '/' (Unix) or contain ':\\' or ':/' (Windows)
- Relative paths: resolved against settings.effective_workspace_dir
"""

from pathlib import Path
from typing import Callable, Optional, Union

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

    return str(settings.effective_workspace_dir / path)


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

    # Otherwise resolve against the configured workspace
    return str(settings.effective_workspace_dir / worktree_path)


def get_worktree_working_dir_as_path(worktree: dict) -> Path:
    """Resolve worktree working directory as a Path object."""
    return Path(get_worktree_working_dir(worktree))


def resolve_existing_worktree_cwd(
    worktree: Optional[dict],
    session: Optional[dict],
    is_dir: Optional[Callable[[str], bool]] = None,
) -> Optional[str]:
    """Resolve the first worktree cwd candidate whose directory EXISTS on disk.

    The shared rule for every worktree-session path consumer (resume cwd
    resolution, history/JSONL readers, session-poll metadata): a worktree session
    only needs its working directory to exist — the worktree row's ``status`` is
    never consulted.

    Candidates, in order:
      1. the worktree row's ``worktree_path``;
      2. the session's backup ``worktree_path`` column. This is a write-once
         snapshot while adopt/recreate rewrite the ROW path, so the two can
         genuinely diverge — both must be tried.

    Each candidate resolves independently (per-candidate try/except), so a
    corrupt/unresolvable first candidate never masks a valid second one. This
    function never raises.

    Args:
        worktree: The worktree row, or None.
        session: The session row (carries the backup columns), or None.
        is_dir: Optional existence predicate override taking the resolved
            absolute path (used by pollers to inject a TTL-cached stat).
            Defaults to ``Path(resolved).is_dir()``.

    Returns:
        The resolved absolute path of the first candidate whose directory
        exists, or None when no candidate resolves to an existing directory.
    """
    check = is_dir if is_dir is not None else (lambda p: Path(p).is_dir())
    candidates = []
    if worktree and worktree.get("worktree_path"):
        candidates.append(worktree)
    if session and session.get("worktree_path"):
        candidates.append({
            "worktree_path": session["worktree_path"],
            "id": session.get("worktree_id") or (worktree or {}).get("id") or "unknown",
        })
    seen = set()
    for candidate in candidates:
        try:
            resolved = get_worktree_working_dir(candidate)
            if resolved in seen:
                continue
            seen.add(resolved)
            if check(resolved):
                return resolved
        except Exception:  # noqa: BLE001 — a bad candidate must not mask the next one
            continue
    return None


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

    return settings.effective_workspace_dir
