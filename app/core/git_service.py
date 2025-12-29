"""
Git Service

Provides git operations for repository management, branch operations,
worktree management, and commit graph visualization.

This service wraps git CLI commands and provides a clean interface
for the API layer to interact with git repositories.
"""

import logging
import subprocess
import os
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime

logger = logging.getLogger(__name__)


class GitService:
    """
    Service for git repository operations.

    Provides methods for:
    - Repository introspection (is_git_repo, get_remote_url, get_current_branch)
    - Branch operations (list, create, delete, checkout)
    - Worktree management (list, add, remove)
    - Commit graph and status
    """

    def _run_git(
        self,
        working_dir: str,
        args: List[str],
        timeout: int = 30,
        check: bool = False
    ) -> subprocess.CompletedProcess:
        """
        Run a git command and return the result.

        Args:
            working_dir: Directory to run the command in
            args: Git command arguments (without 'git' prefix)
            timeout: Command timeout in seconds
            check: If True, raise CalledProcessError on non-zero exit

        Returns:
            CompletedProcess with returncode, stdout, stderr
        """
        try:
            return subprocess.run(
                ["git"] + args,
                cwd=working_dir,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=check
            )
        except subprocess.TimeoutExpired:
            logger.warning(f"Git command timed out: git {' '.join(args)}")
            raise
        except subprocess.CalledProcessError as e:
            logger.warning(f"Git command failed: git {' '.join(args)}: {e.stderr}")
            raise

    # =========================================================================
    # Repository Introspection
    # =========================================================================

    def is_git_repo(self, working_dir: str) -> bool:
        """Check if the working directory is a git repository."""
        try:
            result = self._run_git(working_dir, ["rev-parse", "--git-dir"], timeout=5)
            return result.returncode == 0
        except Exception:
            return False

    def get_remote_url(self, working_dir: str) -> Optional[str]:
        """Get the remote URL for origin."""
        try:
            result = self._run_git(working_dir, ["remote", "get-url", "origin"], timeout=5)
            if result.returncode == 0:
                return result.stdout.strip()
            return None
        except Exception:
            return None

    def get_current_branch(self, working_dir: str) -> Optional[str]:
        """Get the current branch name."""
        try:
            result = self._run_git(working_dir, ["rev-parse", "--abbrev-ref", "HEAD"], timeout=5)
            if result.returncode == 0:
                branch = result.stdout.strip()
                return branch if branch != "HEAD" else None
            return None
        except Exception:
            return None

    def get_default_branch(self, working_dir: str) -> str:
        """
        Get the default branch name (usually main or master).
        Checks remote HEAD first, falls back to common names.
        """
        try:
            # Try to get remote HEAD
            result = self._run_git(
                working_dir,
                ["symbolic-ref", "refs/remotes/origin/HEAD", "--short"],
                timeout=5
            )
            if result.returncode == 0:
                # Returns something like "origin/main"
                return result.stdout.strip().replace("origin/", "")

            # Fallback: check if main or master exists
            for branch in ["main", "master"]:
                result = self._run_git(
                    working_dir,
                    ["rev-parse", "--verify", f"refs/heads/{branch}"],
                    timeout=5
                )
                if result.returncode == 0:
                    return branch

            return "main"  # Default fallback
        except Exception:
            return "main"

    def fetch(self, working_dir: str, remote: str = "origin") -> bool:
        """
        Fetch from remote.

        Args:
            working_dir: Repository directory
            remote: Remote name (default: origin)

        Returns:
            True if successful, False otherwise
        """
        try:
            result = self._run_git(
                working_dir,
                ["fetch", remote, "--prune"],
                timeout=60
            )
            if result.returncode == 0:
                logger.info(f"Fetched from {remote}")
                return True
            logger.warning(f"Fetch failed: {result.stderr}")
            return False
        except Exception as e:
            logger.error(f"Fetch error: {e}")
            return False

    # =========================================================================
    # Branch Operations
    # =========================================================================

    def list_branches(
        self,
        working_dir: str,
        include_remote: bool = True
    ) -> List[Dict[str, Any]]:
        """
        List all branches in the repository.

        Args:
            working_dir: Repository directory
            include_remote: Include remote tracking branches

        Returns:
            List of branch dictionaries with:
            - name: Branch name
            - is_current: True if this is the current branch
            - is_remote: True if this is a remote tracking branch
            - commit: Short commit SHA
            - commit_message: First line of commit message
            - ahead: Commits ahead of tracking branch (local only)
            - behind: Commits behind tracking branch (local only)
        """
        try:
            # Use for-each-ref for structured output
            format_str = "%(refname:short)|%(objectname:short)|%(subject)|%(HEAD)|%(upstream:short)|%(upstream:track)"
            args = [
                "for-each-ref",
                f"--format={format_str}",
                "refs/heads/"
            ]
            if include_remote:
                args.append("refs/remotes/")

            result = self._run_git(working_dir, args, timeout=10)
            if result.returncode != 0:
                logger.warning(f"Failed to list branches: {result.stderr}")
                return []

            branches = []
            for line in result.stdout.strip().split("\n"):
                if not line:
                    continue

                parts = line.split("|", 5)
                if len(parts) < 4:
                    continue

                name = parts[0]
                commit = parts[1]
                message = parts[2] if len(parts) > 2 else ""
                is_current = parts[3] == "*" if len(parts) > 3 else False
                upstream = parts[4] if len(parts) > 4 else ""
                track_info = parts[5] if len(parts) > 5 else ""

                # Skip HEAD reference
                if name == "origin/HEAD":
                    continue

                is_remote = name.startswith("origin/") or "/" in name

                # Parse ahead/behind from track info like "[ahead 1, behind 2]"
                ahead = 0
                behind = 0
                if track_info:
                    if "ahead" in track_info:
                        try:
                            ahead = int(track_info.split("ahead ")[1].split(",")[0].split("]")[0])
                        except (IndexError, ValueError):
                            pass
                    if "behind" in track_info:
                        try:
                            behind = int(track_info.split("behind ")[1].split("]")[0])
                        except (IndexError, ValueError):
                            pass

                branches.append({
                    "name": name,
                    "is_current": is_current,
                    "is_remote": is_remote,
                    "commit": commit,
                    "commit_message": message[:100],
                    "upstream": upstream if upstream else None,
                    "ahead": ahead,
                    "behind": behind
                })

            # Sort: current first, then local branches, then remote
            branches.sort(key=lambda b: (
                not b["is_current"],
                b["is_remote"],
                b["name"].lower()
            ))

            return branches

        except Exception as e:
            logger.error(f"Error listing branches: {e}")
            return []

    def create_branch(
        self,
        working_dir: str,
        name: str,
        start_point: Optional[str] = None
    ) -> bool:
        """
        Create a new branch.

        Args:
            working_dir: Repository directory
            name: New branch name
            start_point: Starting commit/branch (default: HEAD)

        Returns:
            True if successful, False otherwise
        """
        try:
            args = ["branch", name]
            if start_point:
                args.append(start_point)

            result = self._run_git(working_dir, args, timeout=10)
            if result.returncode == 0:
                logger.info(f"Created branch: {name}")
                return True

            logger.warning(f"Failed to create branch {name}: {result.stderr}")
            return False
        except Exception as e:
            logger.error(f"Error creating branch {name}: {e}")
            return False

    def delete_branch(
        self,
        working_dir: str,
        name: str,
        force: bool = False
    ) -> bool:
        """
        Delete a branch.

        Args:
            working_dir: Repository directory
            name: Branch name to delete
            force: Force delete even if not fully merged

        Returns:
            True if successful, False otherwise
        """
        try:
            flag = "-D" if force else "-d"
            result = self._run_git(working_dir, ["branch", flag, name], timeout=10)
            if result.returncode == 0:
                logger.info(f"Deleted branch: {name}")
                return True

            logger.warning(f"Failed to delete branch {name}: {result.stderr}")
            return False
        except Exception as e:
            logger.error(f"Error deleting branch {name}: {e}")
            return False

    def checkout(self, working_dir: str, ref: str) -> bool:
        """
        Checkout a branch or commit.

        Args:
            working_dir: Repository directory
            ref: Branch name or commit SHA to checkout

        Returns:
            True if successful, False otherwise
        """
        try:
            result = self._run_git(working_dir, ["checkout", ref], timeout=30)
            if result.returncode == 0:
                logger.info(f"Checked out: {ref}")
                return True

            logger.warning(f"Failed to checkout {ref}: {result.stderr}")
            return False
        except Exception as e:
            logger.error(f"Error checking out {ref}: {e}")
            return False

    # =========================================================================
    # Worktree Operations
    # =========================================================================

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
            args = ["worktree", "add"]

            if new_branch:
                args.extend(["-b", branch, path])
                if base_branch:
                    args.append(base_branch)
            else:
                args.extend([path, branch])

            result = self._run_git(main_dir, args, timeout=30)
            if result.returncode == 0:
                logger.info(f"Added worktree at {path} for branch {branch}")
                return True, None

            error_msg = result.stderr.strip() if result.stderr else "Unknown git error"
            logger.warning(f"Failed to add worktree: {error_msg}")
            return False, error_msg
        except Exception as e:
            logger.error(f"Error adding worktree: {e}")
            return False, str(e)

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
            args = ["worktree", "remove"]
            if force:
                args.append("--force")
            args.append(path)

            result = self._run_git(main_dir, args, timeout=30)
            if result.returncode == 0:
                logger.info(f"Removed worktree at {path}")
                return True

            logger.warning(f"Failed to remove worktree: {result.stderr}")
            return False
        except Exception as e:
            logger.error(f"Error removing worktree: {e}")
            return False

    # =========================================================================
    # Commit Graph and Status
    # =========================================================================

    def get_commit_graph(
        self,
        working_dir: str,
        limit: int = 100,
        branch: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Get commit graph data for visualization.

        Args:
            working_dir: Repository directory
            limit: Maximum number of commits to return
            branch: Specific branch (default: all branches)

        Returns:
            List of commit dictionaries with:
            - sha: Full commit SHA
            - short_sha: Short commit SHA
            - message: Commit message (first line)
            - author: Author name
            - author_email: Author email
            - timestamp: ISO format timestamp
            - parents: List of parent SHAs
            - refs: List of refs pointing to this commit
        """
        try:
            # Format: SHA|short SHA|message|author|email|timestamp|parents|refs
            format_str = "%H|%h|%s|%an|%ae|%aI|%P|%D"
            args = [
                "log",
                f"--format={format_str}",
                f"-{limit}",
                "--all" if not branch else branch
            ]

            result = self._run_git(working_dir, args, timeout=30)
            if result.returncode != 0:
                logger.warning(f"Failed to get commit graph: {result.stderr}")
                return []

            commits = []
            for line in result.stdout.strip().split("\n"):
                if not line:
                    continue

                parts = line.split("|", 7)
                if len(parts) < 6:
                    continue

                sha = parts[0]
                short_sha = parts[1]
                message = parts[2]
                author = parts[3]
                author_email = parts[4]
                timestamp = parts[5]
                parents = parts[6].split() if len(parts) > 6 and parts[6] else []
                refs_str = parts[7] if len(parts) > 7 else ""

                # Parse refs
                refs = []
                if refs_str:
                    for ref in refs_str.split(", "):
                        ref = ref.strip()
                        if ref:
                            refs.append(ref)

                commits.append({
                    "sha": sha,
                    "short_sha": short_sha,
                    "message": message,
                    "author": author,
                    "author_email": author_email,
                    "timestamp": timestamp,
                    "parents": parents,
                    "refs": refs
                })

            return commits

        except Exception as e:
            logger.error(f"Error getting commit graph: {e}")
            return []

    def get_status(self, working_dir: str) -> Dict[str, Any]:
        """
        Get repository status.

        Args:
            working_dir: Repository directory

        Returns:
            Dictionary with:
            - is_git_repo: Whether this is a git repository
            - current_branch: Current branch name
            - is_detached: Whether HEAD is detached
            - head_commit: Current HEAD commit SHA
            - remote_url: Origin remote URL
            - is_clean: True if no uncommitted changes
            - staged: List of staged files
            - modified: List of modified files
            - untracked: List of untracked files
            - ahead: Commits ahead of upstream
            - behind: Commits behind upstream
            - conflicts: List of files with merge conflicts
        """
        status = {
            "is_git_repo": False,
            "current_branch": None,
            "is_detached": False,
            "head_commit": None,
            "remote_url": None,
            "is_clean": True,
            "staged": [],
            "modified": [],
            "untracked": [],
            "ahead": 0,
            "behind": 0,
            "conflicts": []
        }

        if not self.is_git_repo(working_dir):
            return status

        status["is_git_repo"] = True
        status["remote_url"] = self.get_remote_url(working_dir)

        try:
            # Get current branch and HEAD
            result = self._run_git(working_dir, ["rev-parse", "--abbrev-ref", "HEAD"], timeout=5)
            if result.returncode == 0:
                branch = result.stdout.strip()
                if branch == "HEAD":
                    status["is_detached"] = True
                else:
                    status["current_branch"] = branch

            result = self._run_git(working_dir, ["rev-parse", "HEAD"], timeout=5)
            if result.returncode == 0:
                status["head_commit"] = result.stdout.strip()[:12]

            # Get ahead/behind
            if status["current_branch"]:
                result = self._run_git(
                    working_dir,
                    ["rev-list", "--left-right", "--count", f"{status['current_branch']}...@{{upstream}}"],
                    timeout=5
                )
                if result.returncode == 0:
                    parts = result.stdout.strip().split()
                    if len(parts) >= 2:
                        status["ahead"] = int(parts[0])
                        status["behind"] = int(parts[1])

            # Get file status using porcelain format
            result = self._run_git(working_dir, ["status", "--porcelain=v1"], timeout=10)
            if result.returncode == 0:
                for line in result.stdout.split("\n"):
                    if not line or len(line) < 3:
                        continue

                    index_status = line[0]
                    worktree_status = line[1]
                    filename = line[3:]

                    # Check for conflicts (both sides modified)
                    if index_status == "U" or worktree_status == "U":
                        status["conflicts"].append(filename)
                    elif index_status == "A" and worktree_status == "A":
                        status["conflicts"].append(filename)
                    elif index_status == "D" and worktree_status == "D":
                        status["conflicts"].append(filename)
                    # Staged changes
                    elif index_status in ["A", "M", "D", "R", "C"]:
                        status["staged"].append({
                            "file": filename,
                            "status": index_status
                        })
                    # Worktree changes (not staged)
                    if worktree_status == "M":
                        status["modified"].append(filename)
                    elif worktree_status == "?":
                        status["untracked"].append(filename)

                status["is_clean"] = (
                    not status["staged"] and
                    not status["modified"] and
                    not status["untracked"] and
                    not status["conflicts"]
                )

        except Exception as e:
            logger.error(f"Error getting git status: {e}")

        return status


# Global instance
git_service = GitService()
