"""
Git Worktree Operations Mixin

Provides worktree management: list, add, remove, and prune worktrees.
"""

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from app.core.git_cache import _native_reader

logger = logging.getLogger(__name__)


class GitWorktreeMixin:
    """Mixin providing worktree operations for GitService."""

    def list_worktrees(self, working_dir: str) -> List[Dict[str, Any]]:
        """
        List all worktrees for a repository.

        Args:
            working_dir: Main repository directory

        Returns:
            List of worktree dictionaries with:
            - path: Absolute path to worktree
            - head: Current commit SHA
            - branch: Branch name (if on a branch)
            - is_main: True if this is the main worktree
            - is_bare: True if bare repository
            - is_detached: True if HEAD is detached
        """
        # Shared-DB clusters (a primary + a worker sharing one Postgres) carry
        # project/worktree rows whose repo paths belong to another install — e.g.
        # the primary's Windows path ``D:\Projects\ai-shuffle`` reaching a Linux
        # worker. That path cannot exist locally, so running git with it as cwd
        # raises FileNotFoundError, which previously surfaced as a repeated
        # ERROR-level stack ("Error listing worktrees"). A foreign install's
        # checkout simply has no worktrees here: skip quietly and return empty.
        if not os.path.isdir(working_dir):
            logger.debug(
                "Skipping worktree listing: repo path not present on this install: %s",
                working_dir,
            )
            return []

        try:
            result = self._run_git(
                working_dir,
                ["worktree", "list", "--porcelain"],
                timeout=10
            )
            if result.returncode != 0:
                logger.warning(f"Failed to list worktrees: {result.stderr}")
                return []

            worktrees = []
            current_worktree: Dict[str, Any] = {}

            for line in result.stdout.split("\n"):
                line = line.strip()
                if not line:
                    if current_worktree:
                        worktrees.append(current_worktree)
                        current_worktree = {}
                    continue

                if line.startswith("worktree "):
                    current_worktree["path"] = line[9:]
                elif line.startswith("HEAD "):
                    current_worktree["head"] = line[5:]
                elif line.startswith("branch "):
                    # Format: branch refs/heads/branch-name
                    branch_ref = line[7:]
                    if branch_ref.startswith("refs/heads/"):
                        current_worktree["branch"] = branch_ref[11:]
                    else:
                        current_worktree["branch"] = branch_ref
                elif line == "bare":
                    current_worktree["is_bare"] = True
                elif line == "detached":
                    current_worktree["is_detached"] = True

            # Don't forget the last worktree
            if current_worktree:
                worktrees.append(current_worktree)

            # Mark the main worktree (first one, at main_dir)
            main_path = os.path.abspath(working_dir)
            for wt in worktrees:
                wt["is_main"] = os.path.abspath(wt.get("path", "")) == main_path
                wt.setdefault("is_bare", False)
                wt.setdefault("is_detached", False)
                wt.setdefault("branch", None)

            return worktrees

        except Exception as e:
            logger.error(f"Error listing worktrees: {e}")
            return []

    def add_worktree(
        self,
        main_dir: str,
        path: str,
        branch: str,
        new_branch: bool = False,
        base_branch: Optional[str] = None
    ) -> Tuple[bool, Optional[str]]:
        """
        Add a new worktree.

        Args:
            main_dir: Main repository directory
            path: Path for the new worktree
            branch: Branch name to checkout
            new_branch: If True, create a new branch
            base_branch: Base branch for new branch (only if new_branch=True)

        Returns:
            Tuple of (success: bool, error_message: Optional[str])
        """
        try:
            # When creating a new branch from a base branch, fetch latest from origin first
            # This ensures the new branch includes all remote commits
            if new_branch and base_branch:
                logger.debug(f"Fetching latest from origin before creating branch from {base_branch}")
                self.fetch(main_dir, "origin")

            args = ["worktree", "add"]

            if new_branch:
                args.extend(["-b", branch, "--", path])
                if base_branch:
                    # Use origin/{base_branch} to ensure we get the latest remote commits
                    # This prevents creating branches from stale local branches
                    remote_base = f"origin/{base_branch}"
                    # Check if the remote branch exists
                    check_result = self._run_git(main_dir, ["rev-parse", "--verify", remote_base], timeout=10)
                    if check_result.returncode == 0:
                        args.append(remote_base)
                        logger.debug(f"Using remote branch {remote_base} as base for new branch {branch}")
                    else:
                        # Fall back to local branch if remote doesn't exist
                        args.append(base_branch)
                        logger.debug(f"Remote {remote_base} not found, using local {base_branch} as base")
            else:
                args.extend(["--", path, branch])

            result = self._run_git(main_dir, args, timeout=30)
            if result.returncode == 0:
                logger.debug(f"Added worktree at {path} for branch {branch}")
                if _native_reader is not None:
                    _native_reader.invalidate_repo(main_dir)
                return True, None

            error_msg = result.stderr.strip() if result.stderr else "Unknown git error"
            logger.warning(f"Failed to add worktree: {error_msg}")
            return False, error_msg
        except Exception as e:
            logger.error(f"Error adding worktree: {e}")
            return False, str(e)

    def _registered_worktree_path(self, main_dir: str, path: str) -> str:
        """Return the path git itself has registered for this worktree.

        ``git worktree remove`` matches the path it recorded, which on Windows
        may differ in slash style or drive-letter case from the path we stored.
        Fall back to ``path`` when listing fails or nothing matches.
        """
        target = os.path.normcase(os.path.normpath(os.path.abspath(path)))
        for wt in self.list_worktrees(main_dir):
            registered = wt.get("path")
            if not registered:
                continue
            if os.path.normcase(os.path.normpath(os.path.abspath(registered))) == target:
                return registered
        return path

    def remove_worktree(
        self,
        main_dir: str,
        path: str,
        force: bool = False
    ) -> bool:
        """
        Remove a worktree.

        Args:
            main_dir: Main repository directory
            path: Path of the worktree to remove
            force: Force removal even if dirty

        Returns:
            True if successful, False otherwise
        """
        try:
            # Detach provisioned runtime links (node_modules, venv, built
            # assets) BEFORE git sees the directory. On Windows they are
            # junctions, and `git worktree remove` recurses through a junction
            # into its target — left in place, removing a provisioned worktree
            # guts the main checkout's node_modules. Local import: this module
            # is a git_service mixin and checkout_manager imports git_service.
            from app.core.checkout_manager import sever_runtime_links

            sever_summary = sever_runtime_links(path)
            if sever_summary["errors"]:
                # Do not hand the directory to git while a junction may still
                # be attached — continuing is how the main checkout's
                # node_modules got deleted.
                logger.warning(
                    "Severing runtime links in %s hit errors; refusing git "
                    "worktree remove: %s",
                    path,
                    sever_summary["errors"],
                )
                return False

            registered = self._registered_worktree_path(main_dir, path)
            args = ["worktree", "remove"]
            if force:
                args.append("--force")
            args.append(registered)

            result = self._run_git(main_dir, args, timeout=30)
            if result.returncode == 0:
                logger.debug(f"Removed worktree at {registered}")
                if _native_reader is not None:
                    _native_reader.invalidate_repo(main_dir)
                return True

            logger.warning(f"Failed to remove worktree: {result.stderr}")
            return False
        except Exception as e:
            logger.error(f"Error removing worktree: {e}")
            return False

    def prune_worktrees(self, main_dir: str) -> bool:
        """
        Prune stale worktree tracking entries.

        Removes git's internal worktree metadata for worktrees whose
        directories no longer exist on disk. This must be called before
        deleting branches that were associated with removed worktrees,
        otherwise git refuses to delete them.

        Args:
            main_dir: Main repository directory

        Returns:
            True if successful, False otherwise
        """
        try:
            result = self._run_git(main_dir, ["worktree", "prune"], timeout=10)
            if result.returncode == 0:
                logger.debug("Pruned stale worktree entries")
                return True

            logger.warning(f"Failed to prune worktrees: {result.stderr}")
            return False
        except Exception as e:
            logger.error(f"Error pruning worktrees: {e}")
            return False
