"""
Worktree Manager

Manages worktree lifecycle independently from chat sessions.

Worktrees are persistent resources that can be used by multiple sessions
over time. The relationship is tracked via sessions.worktree_id.

Key design principles:
- Worktrees are independent of sessions (can exist without a session)
- Multiple sessions can use the same worktree (sequentially, not concurrently)
- Worktree validity is determined by checking if the folder exists on disk

Path pattern:
  Workspace projects: {workspace_dir}/{branch_sanitized}-{short_hash}
  Absolute-path projects: {project_parent}/{branch_sanitized}-{short_hash} (sibling placement)
"""

import hashlib
import logging
import uuid
from pathlib import Path
from typing import Optional, Tuple, Dict, Any, List

from app.core.config import settings
from app.core.git_service import git_service
from app.core.project_utils import (
    get_project_working_dir, get_worktree_working_dir,
    get_worktree_working_dir_as_path, get_project_parent_dir
)
from app.db import database
from app.db.database import WorktreeStatus

logger = logging.getLogger(__name__)


class WorktreeError(Exception):
    """Exception raised for worktree creation/management errors with user-friendly messages."""
    pass


class WorktreeManager:
    """
    Manages worktree lifecycle independently from chat sessions.

    Worktrees are persistent resources that can be used by multiple sessions
    over time. Sessions track their worktree via the sessions.worktree_id field.

    Key operations:
    - create_worktree(): Creates a standalone worktree (no session)
    - create_worktree_session(): Creates worktree + session together (legacy support)
    - get_available_worktrees(): Gets worktrees available for new sessions
    - is_worktree_valid(): Checks if worktree folder exists on disk

    Path pattern: {workspace_dir}/{branch_sanitized}-{short_hash}
    (Worktrees are adjacent to the main repo, not nested inside it)
    """

    def __init__(self):
        self.git_service = git_service

    def _sanitize_branch_name(self, branch: str) -> str:
        """Sanitize branch name for use in filesystem path"""
        # Replace common problematic characters
        return branch.replace("/", "-").replace("\\", "-").replace(":", "-").replace("*", "-")

    def _get_worktree_path(self, project_path: str, branch: str) -> Path:
        """
        Get full path for a worktree - adjacent to the main repo.
        Uses a short hash of the project path to ensure uniqueness across repos.

        For relative-path projects (workspace-based):
            {workspace_dir}/feature-auth-a7b3
        For absolute-path projects (local mode):
            {project_parent}/feature-auth-a7b3  (sibling directory)
        """
        sanitized = self._sanitize_branch_name(branch)

        # Generate a short hash from the project path for uniqueness
        path_hash = hashlib.md5(project_path.encode()).hexdigest()[:4]
        base_name = f"{sanitized}-{path_hash}"

        # Place worktrees in the parent directory of the project
        # For workspace projects: this is workspace_dir
        # For absolute-path projects: this is the project's parent dir (sibling placement)
        parent_dir = get_project_parent_dir(project_path)
        base_path = parent_dir / base_name

        # If base path doesn't exist, use it
        if not base_path.exists():
            return base_path

        # Otherwise, find a unique suffix
        counter = 2
        while True:
            candidate = parent_dir / f"{base_name}-{counter}"
            if not candidate.exists():
                return candidate
            counter += 1
            if counter > 100:
                raise WorktreeError(f"Too many worktrees for branch '{branch}'")

    def _get_relative_worktree_path_from_full(self, full_path: Path, project_path: str = "") -> str:
        """
        Get the worktree path to store in the database.

        For workspace-based projects: just the folder name (relative to workspace_dir).
        For absolute-path projects: the full absolute path.
        """
        from app.core.project_utils import is_absolute_project_path
        if project_path and is_absolute_project_path(project_path):
            return str(full_path)
        return full_path.name

    def _ensure_repository_record(self, project_id: str, project_path: str) -> Optional[Dict]:
        """
        Ensure a git_repositories record exists for this project.
        Creates one if needed.

        Returns:
            Repository record dict or None if not a git repo
        """
        working_dir = get_project_working_dir(project_path)

        if not self.git_service.is_git_repo(working_dir):
            logger.warning(f"Project {project_id} is not a git repository")
            return None

        repo = database.get_git_repository_by_project(project_id)
        if repo:
            return repo

        # Create a new record
        repo_id = f"repo-{uuid.uuid4().hex[:12]}"
        remote_url = self.git_service.get_remote_url(working_dir)
        default_branch = self.git_service.get_default_branch(working_dir)

        # Try to extract GitHub repo name from URL
        github_repo_name = None
        if remote_url and "github.com" in remote_url:
            try:
                if remote_url.startswith("git@"):
                    github_repo_name = remote_url.split(":")[1].replace(".git", "")
                else:
                    parts = remote_url.replace(".git", "").split("github.com/")
                    if len(parts) > 1:
                        github_repo_name = parts[1]
            except Exception:
                pass

        repo = database.create_git_repository(
            repository_id=repo_id,
            project_id=project_id,
            remote_url=remote_url,
            default_branch=default_branch,
            github_repo_name=github_repo_name
        )
        logger.info(f"Created git repository record for project {project_id}: {repo_id}")
        return repo

    def sync_worktrees_for_project(self, project_id: str) -> Dict[str, Any]:
        """
        Sync database worktree records with actual state on disk.

        This should be called when a chat card loads for a git project to ensure
        the database reflects reality (worktrees may have been deleted outside the app).

        Args:
            project_id: The project to sync worktrees for

        Returns:
            Dict with sync results: cleaned_up (list of deleted worktree ids), active (count)
        """
        project = database.get_project(project_id)
        if not project:
            logger.warning(f"Project not found for sync: {project_id}")
            return {"cleaned_up": [], "active": 0, "error": "Project not found"}

        repo = database.get_git_repository_by_project(project_id)
        if not repo:
            # Not a git repo, nothing to sync
            return {"cleaned_up": [], "active": 0, "is_git_repo": False}

        # Get all active worktrees from database
        active_worktrees = database.get_active_worktrees_for_repository(repo["id"])

        cleaned_up = []
        still_active = 0

        main_dir = get_project_working_dir(project)

        for wt in active_worktrees:
            worktree_full_path = get_worktree_working_dir_as_path(wt)

            if not worktree_full_path.exists():
                # Stale record - worktree was deleted outside the app
                branch_name = wt["branch_name"]
                logger.info(f"Cleaning up stale worktree record: {branch_name} (path: {worktree_full_path})")
                database.update_worktree(wt["id"], status=WorktreeStatus.DELETED)

                # Also try to delete the orphaned branch if it exists and is not the default branch
                default_branch = repo.get("default_branch", "main")
                branch_deleted = False
                if branch_name and branch_name != default_branch:
                    try:
                        # Check if the branch exists before trying to delete
                        branches = self.git_service.list_branches(main_dir, include_remote=False)
                        local_branch_names = [b["name"] for b in branches if not b["is_remote"]]
                        if branch_name in local_branch_names:
                            # Delete the orphaned branch
                            success = self.git_service.delete_branch(main_dir, branch_name, force=True)
                            if success:
                                logger.info(f"Deleted orphaned branch: {branch_name}")
                                branch_deleted = True
                            else:
                                logger.warning(f"Failed to delete orphaned branch: {branch_name}")
                    except Exception as e:
                        logger.warning(f"Error cleaning up orphaned branch {branch_name}: {e}")

                cleaned_up.append({
                    "id": wt["id"],
                    "branch_name": branch_name,
                    "reason": "path_not_found",
                    "branch_deleted": branch_deleted
                })
            else:
                still_active += 1

        if cleaned_up:
            logger.info(f"Cleaned up {len(cleaned_up)} stale worktree records for project {project_id}")

        return {
            "cleaned_up": cleaned_up,
            "active": still_active,
            "is_git_repo": True
        }

    def is_worktree_valid(self, worktree: Dict) -> bool:
        """
        Check if a worktree is valid on disk.

        A valid git worktree has:
        1. The directory exists
        2. Contains a .git file (not directory) that points to the main repo

        An empty folder or folder without .git file is NOT a valid worktree.
        """
        if not worktree:
            return False
        worktree_path = get_worktree_working_dir_as_path(worktree)
        if not worktree_path.exists():
            return False

        # Check for .git file (worktrees have a .git FILE, not directory)
        git_file = worktree_path / ".git"
        if not git_file.exists():
            return False

        # .git should be a file (pointing to main repo), not a directory
        if git_file.is_dir():
            return False

        return True

    def create_worktree(
        self,
        project_id: str,
        branch_name: str,
        create_new_branch: bool = False,
        base_branch: Optional[str] = None
    ) -> Dict:
        """
        Create a standalone worktree without an associated session.

        This is the primary method for creating worktrees. Sessions can later
        be linked to this worktree via sessions.worktree_id.

        Args:
            project_id: The project containing the main repo
            branch_name: Branch to checkout in worktree
            create_new_branch: If True, create new branch from base_branch
            base_branch: Base branch for new branch (defaults to current HEAD)

        Returns:
            worktree_dict with id, branch_name, worktree_path, etc.

        Raises:
            WorktreeError: With descriptive message on failure
        """
        # Get project
        project = database.get_project(project_id)
        if not project:
            logger.error(f"Project not found: {project_id}")
            raise WorktreeError(f"Project not found: {project_id}")

        # Ensure repository record exists
        repo = self._ensure_repository_record(project_id, project["path"])
        if not repo:
            logger.error(f"Project {project_id} is not a git repository")
            raise WorktreeError("This project is not a git repository. Initialize git first.")

        main_dir = get_project_working_dir(project)

        # Clean up stale worktree records (where the folder no longer exists)
        existing_worktrees = database.get_active_worktrees_for_repository(repo["id"])
        for wt in existing_worktrees:
            worktree_full_path = get_worktree_working_dir_as_path(wt)
            if not worktree_full_path.exists():
                logger.info(f"Cleaning up stale worktree record: '{wt['worktree_path']}'")
                database.update_worktree(wt["id"], status=WorktreeStatus.DELETED)

        # Verify the branch exists if not creating a new one
        if not create_new_branch:
            branches = self.git_service.list_branches(main_dir, include_remote=False)
            local_branch_names = [b["name"] for b in branches if not b["is_remote"]]
            if branch_name not in local_branch_names:
                remote_branches = self.git_service.list_branches(main_dir, include_remote=True)
                remote_name = f"origin/{branch_name}"
                if not any(b["name"] == remote_name for b in remote_branches):
                    logger.error(f"Branch {branch_name} does not exist")
                    raise WorktreeError(f"Branch '{branch_name}' does not exist.")

        # Check if branch name already exists when creating new branch
        if create_new_branch:
            branches = self.git_service.list_branches(main_dir, include_remote=False)
            local_branch_names = [b["name"] for b in branches if not b["is_remote"]]
            if branch_name in local_branch_names:
                logger.error(f"Branch {branch_name} already exists")
                raise WorktreeError(f"Branch '{branch_name}' already exists. Use existing branch mode.")

        # Calculate worktree path (auto-generates unique name if needed)
        worktree_path = self._get_worktree_path(project["path"], branch_name)
        relative_worktree_path = self._get_relative_worktree_path_from_full(worktree_path, project["path"])

        logger.info(f"Creating worktree at: {worktree_path}")

        # Create git worktree
        success, error_msg = self.git_service.add_worktree(
            main_dir=main_dir,
            path=str(worktree_path),
            branch=branch_name,
            new_branch=create_new_branch,
            base_branch=base_branch
        )

        if not success:
            logger.error(f"Failed to create git worktree: {error_msg}")
            raise WorktreeError(f"Git failed to create worktree: {error_msg or 'Unknown error'}")

        # Create worktree record (no session_id - standalone worktree)
        worktree_id = f"wt-{uuid.uuid4().hex[:12]}"
        try:
            worktree = database.create_worktree(
                worktree_id=worktree_id,
                repository_id=repo["id"],
                branch_name=branch_name,
                worktree_path=relative_worktree_path,
                session_id=None,  # No session - standalone worktree
                base_branch=base_branch,
                status=WorktreeStatus.ACTIVE
            )
        except Exception as e:
            logger.error(f"Failed to create worktree record: {e}")
            self.git_service.remove_worktree(main_dir, str(worktree_path), force=True)
            # Check if this is a unique constraint violation (race condition - another request won)
            error_msg = str(e).lower()
            if "unique constraint" in error_msg or "unique" in error_msg:
                # Another concurrent request created the worktree first
                # Return the existing worktree instead of failing
                existing = database.get_worktree_by_branch(repo["id"], branch_name)
                if existing and existing.get("status") == WorktreeStatus.ACTIVE:
                    logger.info(f"Concurrent creation detected, returning existing worktree for branch '{branch_name}'")
                    return existing
            raise WorktreeError(f"Failed to create worktree record: {str(e)}")

        logger.info(f"Created standalone worktree {worktree_id} at {worktree_path} for branch {branch_name}")
        return worktree

    def get_available_worktrees(self, project_id: str) -> List[Dict]:
        """
        Get all valid worktrees for a project that can be used for new sessions.

        Returns worktrees with their current state:
        - exists: Whether the folder exists on disk
        - sessions: List of sessions that have used this worktree
        - active_session: Currently active session (if any)

        Args:
            project_id: The project ID

        Returns:
            List of worktree dicts with enriched info
        """
        repo = database.get_git_repository_by_project(project_id)
        if not repo:
            return []

        worktrees = database.get_active_worktrees_for_repository(repo["id"])
        result = []

        for wt in worktrees:
            # Check if worktree is valid (not just if folder exists)
            wt["exists"] = self.is_worktree_valid(wt)

            if not wt["exists"]:
                # Invalid worktree (missing, empty, or corrupted) - skip it
                continue

            # Get all sessions for this worktree
            sessions = database.get_sessions_for_worktree(wt["id"])
            wt["sessions"] = sessions
            wt["session_count"] = len(sessions)

            # Find active session (if any)
            active_sessions = [s for s in sessions if s.get("status") == WorktreeStatus.ACTIVE]
            wt["active_session"] = active_sessions[0] if active_sessions else None

            result.append(wt)

        return result

    def create_worktree_session(
        self,
        project_id: str,
        branch_name: str,
        create_new_branch: bool = False,
        base_branch: Optional[str] = None,
        profile_id: Optional[str] = None
    ) -> Tuple[Optional[Dict], Optional[Dict]]:
        """
        Create a new worktree and associated chat session.

        Args:
            project_id: The project containing the main repo
            branch_name: Branch to checkout in worktree
            create_new_branch: If True, create new branch
            base_branch: Base branch for new branch (defaults to current)
            profile_id: Profile for the new session

        Returns:
            Tuple of (worktree_dict, session_dict)

        Raises:
            WorktreeError: With descriptive message on failure
        """
        # Get project
        project = database.get_project(project_id)
        if not project:
            logger.error(f"Project not found: {project_id}")
            raise WorktreeError(f"Project not found: {project_id}")

        # Ensure repository record exists
        repo = self._ensure_repository_record(project_id, project["path"])
        if not repo:
            logger.error(f"Project {project_id} is not a git repository")
            raise WorktreeError("This project is not a git repository. Initialize git first.")

        main_dir = get_project_working_dir(project)

        # Clean up stale worktree records (where the worktree is no longer valid on disk)
        existing_worktrees = database.get_active_worktrees_for_repository(repo["id"])
        for wt in existing_worktrees:
            if not self.is_worktree_valid(wt):
                logger.info(f"Cleaning up stale worktree record: '{wt['worktree_path']}' (invalid worktree)")
                database.update_worktree(wt["id"], status=WorktreeStatus.DELETED)

        # Verify the branch exists if not creating a new one
        if not create_new_branch:
            branches = self.git_service.list_branches(main_dir, include_remote=False)
            local_branch_names = [b["name"] for b in branches if not b["is_remote"]]
            if branch_name not in local_branch_names:
                # Try to find as a remote branch
                remote_branches = self.git_service.list_branches(main_dir, include_remote=True)
                remote_name = f"origin/{branch_name}"
                if not any(b["name"] == remote_name for b in remote_branches):
                    logger.error(f"Branch {branch_name} does not exist")
                    raise WorktreeError(f"Branch '{branch_name}' does not exist. Create the branch first or enable 'create new branch'.")

        # Check if branch name already exists when creating new branch
        if create_new_branch:
            branches = self.git_service.list_branches(main_dir, include_remote=False)
            local_branch_names = [b["name"] for b in branches if not b["is_remote"]]
            if branch_name in local_branch_names:
                logger.error(f"Branch {branch_name} already exists")
                raise WorktreeError(f"Branch '{branch_name}' already exists. Use a different name or select 'existing branch' mode.")

        # Calculate worktree path (auto-generates unique name if needed)
        worktree_path = self._get_worktree_path(project["path"], branch_name)
        relative_worktree_path = self._get_relative_worktree_path_from_full(worktree_path, project["path"])

        logger.info(f"Creating worktree at: {worktree_path}")

        # Create git worktree
        success, error_msg = self.git_service.add_worktree(
            main_dir=main_dir,
            path=str(worktree_path),
            branch=branch_name,
            new_branch=create_new_branch,
            base_branch=base_branch
        )

        if not success:
            logger.error(f"Failed to create git worktree for branch {branch_name}: {error_msg}")
            raise WorktreeError(f"Git failed to create worktree: {error_msg or 'Unknown git error'}")

        # Determine the profile ID for the session
        if not profile_id:
            # Use the default profile from project settings or a fallback
            project_settings = project.get("settings", {})
            profile_id = project_settings.get("default_profile_id")
            if not profile_id:
                # Use a system default profile
                profiles = database.get_all_profiles()
                if profiles:
                    profile_id = profiles[0]["id"]
                else:
                    logger.error("No profiles available for session creation")
                    # Clean up the created worktree
                    self.git_service.remove_worktree(main_dir, str(worktree_path), force=True)
                    raise WorktreeError("No profiles available. Please create a profile first.")

        # Create worktree record FIRST (worktrees are independent of sessions)
        worktree_id = f"wt-{uuid.uuid4().hex[:12]}"
        try:
            worktree = database.create_worktree(
                worktree_id=worktree_id,
                repository_id=repo["id"],
                branch_name=branch_name,
                worktree_path=relative_worktree_path,
                session_id=None,  # New model: no session_id on worktree
                base_branch=base_branch,
                status=WorktreeStatus.ACTIVE
            )
        except Exception as e:
            logger.error(f"Failed to create worktree record: {e}")
            self.git_service.remove_worktree(main_dir, str(worktree_path), force=True)
            # Check if this is a unique constraint violation (race condition)
            error_msg = str(e).lower()
            if "unique constraint" in error_msg or "unique" in error_msg:
                raise WorktreeError(f"Branch '{branch_name}' already has an active worktree (created by concurrent request).")
            raise WorktreeError(f"Failed to create worktree record: {str(e)}")

        # Create session with worktree_id (new relationship model)
        session_id = f"ses-{uuid.uuid4().hex[:12]}"
        session_title = f"Branch: {branch_name}"

        try:
            session = database.create_session(
                session_id=session_id,
                profile_id=profile_id,
                project_id=project_id,
                title=session_title,
                worktree_id=worktree_id,  # Link session to worktree via new model
                worktree_branch=branch_name,
                worktree_path=relative_worktree_path
            )
        except Exception as e:
            logger.error(f"Failed to create session: {e}")
            # Clean up the created worktree
            self.git_service.remove_worktree(main_dir, str(worktree_path), force=True)
            database.delete_worktree(worktree_id)
            raise WorktreeError(f"Failed to create chat session: {str(e)}")

        logger.info(f"Created worktree {worktree_id} at {worktree_path} for branch {branch_name} with session {session_id}")
        return worktree, session

    def get_worktree_by_session(self, session_id: str) -> Optional[Dict]:
        """Get worktree associated with a session"""
        return database.get_worktree_by_session(session_id)

    def get_worktrees_for_project(self, project_id: str) -> List[Dict]:
        """Get all worktrees for a project with session info.

        Uses the new sessions.worktree_id relationship model.
        Uses a single query to avoid N+1 pattern.
        Returns worktrees enriched with:
        - exists: whether worktree folder exists on disk
        - sessions: list of all sessions for this worktree
        - session_count: total number of sessions
        - active_session: currently active session (if any)
        """
        repo = database.get_git_repository_by_project(project_id)
        if not repo:
            return []

        # Use optimized single-query function to avoid N+1 pattern
        worktrees = database.get_worktrees_with_sessions(repo["id"])

        for wt in worktrees:
            # Check if worktree is valid (not just if folder exists)
            wt["exists"] = self.is_worktree_valid(wt)

            # Legacy support: also check deprecated worktrees.session_id if no sessions found
            if not wt["sessions"] and wt.get("session_id"):
                session = database.get_session(wt["session_id"])
                if session:
                    wt["sessions"] = [{
                        "id": session["id"],
                        "title": session["title"],
                        "status": session["status"],
                        "updated_at": session["updated_at"]
                    }]
                    wt["session_count"] = 1
                    if session.get("status") == "active":
                        wt["active_session"] = wt["sessions"][0]

        return worktrees

    def get_worktree_details(self, worktree_id: str) -> Optional[Dict]:
        """Get detailed worktree information with session history."""
        worktree = database.get_worktree(worktree_id)
        if not worktree:
            return None

        # Check if worktree is valid (not just if folder exists)
        worktree_abs_path = get_worktree_working_dir(worktree)
        worktree["exists"] = self.is_worktree_valid(worktree)

        if worktree["exists"]:
            worktree["git_status"] = self.git_service.get_status(worktree_abs_path)

        # Get all sessions for this worktree (new model)
        sessions = database.get_sessions_for_worktree(worktree_id)
        worktree["sessions"] = [{
            "id": s["id"],
            "title": s["title"],
            "status": s["status"],
            "updated_at": s["updated_at"]
        } for s in sessions]
        worktree["session_count"] = len(sessions)

        # Find active session
        active_sessions = [s for s in sessions if s.get("status") == WorktreeStatus.ACTIVE]
        worktree["active_session"] = {
            "id": active_sessions[0]["id"],
            "title": active_sessions[0]["title"],
            "status": active_sessions[0]["status"],
            "updated_at": active_sessions[0]["updated_at"]
        } if active_sessions else None

        # Legacy support: check deprecated worktrees.session_id
        if not worktree["sessions"] and worktree.get("session_id"):
            session = database.get_session(worktree["session_id"])
            if session:
                worktree["sessions"] = [{
                    "id": session["id"],
                    "title": session["title"],
                    "status": session["status"],
                    "updated_at": session["updated_at"]
                }]
                worktree["session_count"] = 1
                if session.get("status") == WorktreeStatus.ACTIVE:
                    worktree["active_session"] = worktree["sessions"][0]

        return worktree

    def cleanup_worktree(self, worktree_id: str, keep_branch: bool = True) -> bool:
        """
        Remove a worktree and optionally delete its branch.

        Args:
            worktree_id: The worktree to remove
            keep_branch: If True, keep the branch after removing worktree

        Returns:
            True if successful
        """
        worktree = database.get_worktree(worktree_id)
        if not worktree:
            logger.warning(f"Worktree not found: {worktree_id}")
            return False

        # Get repository to find main directory
        repo = database.get_git_repository(worktree["repository_id"])
        if not repo:
            logger.warning(f"Repository not found for worktree: {worktree_id}")
            # Still try to clean up the database record
            database.delete_worktree(worktree_id)
            return True

        # Get project to find main directory
        project = database.get_project(repo["project_id"])
        if not project:
            logger.warning(f"Project not found for repository: {repo['id']}")
            database.delete_worktree(worktree_id)
            return True

        main_dir = get_project_working_dir(project)
        worktree_abs_path = get_worktree_working_dir(worktree)

        # Remove git worktree
        if Path(worktree_abs_path).exists():
            success = self.git_service.remove_worktree(main_dir, worktree_abs_path, force=True)
            if not success:
                logger.warning(f"Failed to remove git worktree at {worktree_abs_path}")
                # Continue to mark as removed in database

        # Optionally delete the branch
        if not keep_branch:
            branch_name = worktree["branch_name"]
            # Don't delete if it's the default branch
            default_branch = repo.get("default_branch", "main")
            if branch_name != default_branch:
                self.git_service.delete_branch(main_dir, branch_name, force=True)
                logger.info(f"Deleted branch {branch_name}")

        # Update database record
        database.update_worktree(worktree_id, status=WorktreeStatus.DELETED)

        # Delete the worktree record
        database.delete_worktree(worktree_id)

        logger.info(f"Cleaned up worktree {worktree_id}")
        return True

    def sync_worktrees(self, project_id: str) -> Dict[str, Any]:
        """
        Sync database worktrees with actual git worktrees.
        Marks orphaned worktrees and cleans up stale records.

        Returns:
            Dict with sync results
        """
        result = {
            "synced": 0,
            "orphaned": 0,
            "cleaned_up": 0,
            "errors": []
        }

        # Get project and repository
        project = database.get_project(project_id)
        if not project:
            result["errors"].append(f"Project not found: {project_id}")
            return result

        repo = database.get_git_repository_by_project(project_id)
        if not repo:
            result["errors"].append(f"No git repository for project: {project_id}")
            return result

        main_dir = get_project_working_dir(project)

        # Get actual worktrees from git
        git_worktrees = self.git_service.list_worktrees(main_dir)
        git_worktree_paths = set()
        for wt in git_worktrees:
            if not wt.get("is_main"):
                git_worktree_paths.add(wt.get("path", ""))

        # Get database worktrees
        db_worktrees = database.get_worktrees_for_repository(repo["id"])

        for db_wt in db_worktrees:
            worktree_abs_path = get_worktree_working_dir(db_wt)

            if worktree_abs_path in git_worktree_paths:
                # Worktree exists in both git and database
                result["synced"] += 1
            elif db_wt["status"] == WorktreeStatus.ACTIVE:
                # Worktree is in database but not on filesystem - mark as orphaned
                logger.warning(f"Worktree {db_wt['id']} is orphaned (path doesn't exist)")
                database.update_worktree(db_wt["id"], status=WorktreeStatus.ORPHANED)
                result["orphaned"] += 1
            elif db_wt["status"] in [WorktreeStatus.DELETED, WorktreeStatus.ORPHANED]:
                # Clean up stale records
                database.delete_worktree(db_wt["id"])
                result["cleaned_up"] += 1

        # Import external worktrees (created outside the app) into our database
        db_paths = {get_worktree_working_dir(wt) for wt in db_worktrees}
        for git_wt in git_worktrees:
            if git_wt.get("is_main"):
                continue  # Skip main worktree

            git_path = git_wt.get("path", "")
            if git_path and git_path not in db_paths:
                # External worktree found - import it
                try:
                    # Extract relative path (folder name) from absolute path
                    worktree_folder = Path(git_path).name
                    branch_name = git_wt.get("branch", "unknown")

                    # Create database record for this external worktree
                    worktree_id = f"wt-{uuid.uuid4().hex[:12]}"
                    database.create_worktree(
                        worktree_id=worktree_id,
                        repository_id=repo["id"],
                        branch_name=branch_name,
                        worktree_path=worktree_folder,
                        session_id=None,
                        base_branch=None,
                        status=WorktreeStatus.ACTIVE
                    )
                    logger.info(f"Imported external worktree: {worktree_folder} (branch: {branch_name})")
                    result["imported"] = result.get("imported", 0) + 1
                except Exception as e:
                    logger.warning(f"Failed to import external worktree {git_path}: {e}")
                    result["errors"].append(f"Failed to import worktree at {git_path}: {str(e)}")

        logger.info(f"Sync complete for project {project_id}: {result}")
        return result


# Singleton instance
worktree_manager = WorktreeManager()
