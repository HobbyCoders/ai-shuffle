"""
Worktree Manager

Manages worktree lifecycle and links them to chat sessions.

Worktrees allow parallel development on different branches
while sharing the same git repository.

Path pattern: {workspace_dir}/.worktrees/{project_id}/{branch_sanitized}
"""

import logging
import uuid
from pathlib import Path
from typing import Optional, Tuple, Dict, Any, List

from app.core.config import settings
from app.core.git_service import GitService, git_service
from app.db import database

logger = logging.getLogger(__name__)


class WorktreeManager:
    """
    Manages worktree lifecycle and links them to chat sessions.

    Worktrees allow parallel development on different branches
    while sharing the same git repository.

    Path pattern: {workspace_dir}/.worktrees/{project_id}/{branch_sanitized}
    """

    def __init__(self):
        self.git_service = git_service

    def _get_worktree_base_dir(self, project_id: str) -> Path:
        """Get base directory for project worktrees"""
        return settings.workspace_dir / ".worktrees" / project_id

    def _sanitize_branch_name(self, branch: str) -> str:
        """Sanitize branch name for use in filesystem path"""
        # Replace common problematic characters
        return branch.replace("/", "-").replace("\\", "-").replace(":", "-").replace("*", "-")

    def _get_worktree_path(self, project_id: str, branch: str) -> Path:
        """Get full path for a worktree"""
        sanitized = self._sanitize_branch_name(branch)
        return self._get_worktree_base_dir(project_id) / sanitized

    def _get_relative_worktree_path(self, project_id: str, branch: str) -> str:
        """Get relative worktree path (relative to workspace_dir)"""
        sanitized = self._sanitize_branch_name(branch)
        return f".worktrees/{project_id}/{sanitized}"

    def _ensure_repository_record(self, project_id: str, project_path: str) -> Optional[Dict]:
        """
        Ensure a git_repositories record exists for this project.
        Creates one if needed.

        Returns:
            Repository record dict or None if not a git repo
        """
        working_dir = str(settings.workspace_dir / project_path)

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
            Tuple of (worktree_dict, session_dict) or (None, None) on failure
        """
        # Get project
        project = database.get_project(project_id)
        if not project:
            logger.error(f"Project not found: {project_id}")
            return None, None

        # Ensure repository record exists
        repo = self._ensure_repository_record(project_id, project["path"])
        if not repo:
            logger.error(f"Project {project_id} is not a git repository")
            return None, None

        main_dir = str(settings.workspace_dir / project["path"])

        # Check if branch already has a worktree
        existing_worktrees = database.get_active_worktrees_for_repository(repo["id"])
        for wt in existing_worktrees:
            if wt["branch_name"] == branch_name:
                logger.error(f"Branch {branch_name} already has an active worktree")
                return None, None

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
                    return None, None

        # Calculate worktree path
        worktree_path = self._get_worktree_path(project_id, branch_name)
        relative_worktree_path = self._get_relative_worktree_path(project_id, branch_name)

        # Check if path already exists
        if worktree_path.exists():
            logger.error(f"Worktree path already exists: {worktree_path}")
            return None, None

        # Ensure base directory exists
        worktree_path.parent.mkdir(parents=True, exist_ok=True)

        # Create git worktree
        success = self.git_service.add_worktree(
            main_dir=main_dir,
            path=str(worktree_path),
            branch=branch_name,
            new_branch=create_new_branch,
            base_branch=base_branch
        )

        if not success:
            logger.error(f"Failed to create git worktree for branch {branch_name}")
            return None, None

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
                    return None, None

        # Create session
        session_id = f"ses-{uuid.uuid4().hex[:12]}"
        session_title = f"Branch: {branch_name}"

        try:
            session = database.create_session(
                session_id=session_id,
                profile_id=profile_id,
                project_id=project_id,
                title=session_title
            )
        except Exception as e:
            logger.error(f"Failed to create session: {e}")
            # Clean up the created worktree
            self.git_service.remove_worktree(main_dir, str(worktree_path), force=True)
            return None, None

        # Create worktree record
        worktree_id = f"wt-{uuid.uuid4().hex[:12]}"
        try:
            worktree = database.create_worktree(
                worktree_id=worktree_id,
                repository_id=repo["id"],
                branch_name=branch_name,
                worktree_path=relative_worktree_path,
                session_id=session_id,
                base_branch=base_branch,
                status="active"
            )
        except Exception as e:
            logger.error(f"Failed to create worktree record: {e}")
            # Clean up the created worktree and session
            self.git_service.remove_worktree(main_dir, str(worktree_path), force=True)
            database.delete_session(session_id)
            return None, None

        logger.info(f"Created worktree {worktree_id} at {worktree_path} for branch {branch_name}")
        return worktree, session

    def get_worktree_by_session(self, session_id: str) -> Optional[Dict]:
        """Get worktree associated with a session"""
        return database.get_worktree_by_session(session_id)

    def get_worktrees_for_project(self, project_id: str) -> List[Dict]:
        """Get all worktrees for a project"""
        # Get repository for this project
        repo = database.get_git_repository_by_project(project_id)
        if not repo:
            return []

        worktrees = database.get_worktrees_for_repository(repo["id"])

        # Enrich with session info
        for wt in worktrees:
            if wt.get("session_id"):
                session = database.get_session(wt["session_id"])
                if session:
                    wt["session"] = {
                        "id": session["id"],
                        "title": session["title"],
                        "status": session["status"],
                        "updated_at": session["updated_at"]
                    }

        return worktrees

    def get_worktree_details(self, worktree_id: str) -> Optional[Dict]:
        """Get detailed worktree information"""
        worktree = database.get_worktree(worktree_id)
        if not worktree:
            return None

        # Get associated session
        if worktree.get("session_id"):
            session = database.get_session(worktree["session_id"])
            if session:
                worktree["session"] = {
                    "id": session["id"],
                    "title": session["title"],
                    "status": session["status"],
                    "updated_at": session["updated_at"]
                }

        # Get git status for the worktree
        worktree_abs_path = str(settings.workspace_dir / worktree["worktree_path"])
        if Path(worktree_abs_path).exists():
            worktree["git_status"] = self.git_service.get_status(worktree_abs_path)
            worktree["exists"] = True
        else:
            worktree["exists"] = False

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

        main_dir = str(settings.workspace_dir / project["path"])
        worktree_abs_path = str(settings.workspace_dir / worktree["worktree_path"])

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
        database.update_worktree(worktree_id, status="removed")

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

        main_dir = str(settings.workspace_dir / project["path"])

        # Get actual worktrees from git
        git_worktrees = self.git_service.list_worktrees(main_dir)
        git_worktree_paths = set()
        for wt in git_worktrees:
            if not wt.get("is_main"):
                git_worktree_paths.add(wt.get("path", ""))

        # Get database worktrees
        db_worktrees = database.get_worktrees_for_repository(repo["id"])

        for db_wt in db_worktrees:
            worktree_abs_path = str(settings.workspace_dir / db_wt["worktree_path"])

            if worktree_abs_path in git_worktree_paths:
                # Worktree exists in both git and database
                result["synced"] += 1
            elif db_wt["status"] == "active":
                # Worktree is in database but not on filesystem - mark as orphaned
                logger.warning(f"Worktree {db_wt['id']} is orphaned (path doesn't exist)")
                database.update_worktree(db_wt["id"], status="orphaned")
                result["orphaned"] += 1
            elif db_wt["status"] in ["removed", "orphaned"]:
                # Clean up stale records
                database.delete_worktree(db_wt["id"])
                result["cleaned_up"] += 1

        # Check for git worktrees not in database (external worktrees)
        db_paths = {str(settings.workspace_dir / wt["worktree_path"]) for wt in db_worktrees}
        for git_path in git_worktree_paths:
            if git_path not in db_paths:
                logger.info(f"Found external worktree not tracked in database: {git_path}")
                # We don't auto-add these - they might be managed externally

        logger.info(f"Sync complete for project {project_id}: {result}")
        return result


# Singleton instance
worktree_manager = WorktreeManager()
