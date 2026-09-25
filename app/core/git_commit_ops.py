"""
Git Commit Operations Mixin

Provides commit-related git operations: stage, unstage, discard,
commit, push, pull, stash, and conflict detection.
"""

import logging
import os
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _untracked_discard_target(
    working_dir: str, real_working_dir: str, rel_path: str
) -> Optional[str]:
    """Return the path to delete when discarding untracked *rel_path*, or None.

    Only the final component is left unresolved, so an untracked symlink is
    removed itself rather than the directory it points at. The target must sit
    strictly inside the checkout and outside its ``.git`` metadata: discarding
    ``.`` or ``.git`` would otherwise delete the whole checkout or its history.
    """
    lexical = os.path.normpath(os.path.join(working_dir, rel_path))
    target = os.path.join(
        os.path.realpath(os.path.dirname(lexical)), os.path.basename(lexical)
    )
    if not target.startswith(real_working_dir + os.sep):
        return None
    first = os.path.relpath(target, real_working_dir).split(os.sep)[0]
    if first.lower() == ".git":
        return None
    return target


def _push_branch_arg(branch: str) -> str:
    """The refspec argv element for pushing *branch*.

    ``git push`` parses a leading ``-`` as an option even in refspec position,
    and a branch literally named ``--mirror`` (``git init --initial-branch``,
    ``git update-ref``, or a cloned remote HEAD) or a caller-supplied
    ``--receive-pack=<cmd>`` would otherwise reach git as a flag. The fully
    qualified ref names the same branch and can never parse as an option.
    """
    return f"refs/heads/{branch}" if branch.startswith("-") else branch


class GitCommitMixin:
    """Mixin providing commit/staging operations for GitService."""

    def stage_files(self, working_dir: str, files: List[str]) -> bool:
        """
        Stage files for commit.

        Args:
            working_dir: Repository directory
            files: List of file paths to stage (use ['.'] for all)

        Returns:
            True if successful, False otherwise
        """
        try:
            args = ["add", "--"] + files
            result = self._run_git(working_dir, args, timeout=30)

            if result.returncode == 0:
                logger.debug(f"Staged {len(files)} file(s)")
                self.invalidate_status_cache(working_dir)
                return True

            logger.warning(f"Failed to stage files: {result.stderr}")
            return False
        except Exception as e:
            logger.error(f"Error staging files: {e}")
            return False

    def unstage_files(self, working_dir: str, files: List[str]) -> bool:
        """
        Unstage files (remove from index, keep working tree changes).

        Args:
            working_dir: Repository directory
            files: List of file paths to unstage (use ['.'] for all)

        Returns:
            True if successful, False otherwise
        """
        try:
            args = ["reset", "HEAD", "--"] + files
            result = self._run_git(working_dir, args, timeout=30)

            if result.returncode == 0:
                logger.debug(f"Unstaged {len(files)} file(s)")
                self.invalidate_status_cache(working_dir)
                return True

            logger.warning(f"Failed to unstage files: {result.stderr}")
            return False
        except Exception as e:
            logger.error(f"Error unstaging files: {e}")
            return False

    def discard_changes(
        self,
        working_dir: str,
        files: List[str],
        file_statuses: Optional[Dict[str, str]] = None,
    ) -> Tuple[bool, List[str]]:
        """
        Discard changes for specific files.

        Routes to the appropriate git command based on file status:
        - modified (tracked, unstaged): git checkout -- <file>
        - staged: git reset HEAD -- <file>, then git checkout -- <file>
        - untracked: delete the file from disk

        Args:
            working_dir: Repository directory
            files: List of file paths to discard
            file_statuses: Map of file path -> category ('modified', 'staged', 'untracked').
                          If not provided, auto-detects from git status.

        Returns:
            Tuple of (overall_success, list_of_failed_files)
        """
        failed: List[str] = []
        real_working_dir = os.path.realpath(working_dir)

        # Auto-detect statuses if not provided
        if not file_statuses:
            status = self.get_status(working_dir)
            file_statuses = {}
            for sf in status.get("staged", []):
                fname = sf["file"] if isinstance(sf, dict) else sf
                file_statuses[fname] = "staged"
            for f in status.get("modified", []):
                file_statuses[f] = "modified"
            for f in status.get("untracked", []):
                file_statuses[f] = "untracked"

        # Group files by operation
        staged = [f for f in files if file_statuses.get(f) == "staged"]
        modified = [f for f in files if file_statuses.get(f) == "modified"]
        untracked = [f for f in files if file_statuses.get(f) == "untracked"]

        try:
            # Unstage staged files first, then restore
            if staged:
                result = self._run_git(
                    working_dir, ["reset", "HEAD", "--"] + staged, timeout=30
                )
                if result.returncode != 0:
                    logger.warning(f"Failed to reset staged files: {result.stderr}")
                    failed.extend(staged)
                    staged = []  # Don't try checkout on files we couldn't reset

                # Checkout the successfully reset files
                if staged:
                    result = self._run_git(
                        working_dir, ["checkout", "--"] + staged, timeout=30
                    )
                    if result.returncode != 0:
                        logger.warning(f"Failed to checkout staged files: {result.stderr}")
                        failed.extend(staged)

            # Checkout modified files
            if modified:
                result = self._run_git(
                    working_dir, ["checkout", "--"] + modified, timeout=30
                )
                if result.returncode != 0:
                    logger.warning(f"Failed to checkout modified files: {result.stderr}")
                    failed.extend(modified)

            # Delete untracked files (with path traversal protection)
            for f in untracked:
                full_path = _untracked_discard_target(working_dir, real_working_dir, f)
                if full_path is None:
                    logger.warning(f"Refusing to delete path outside the checkout: {f}")
                    failed.append(f)
                    continue
                try:
                    if os.path.islink(full_path):
                        os.remove(full_path)
                    elif os.path.isdir(full_path):
                        import shutil
                        shutil.rmtree(full_path)
                    elif os.path.exists(full_path):
                        os.remove(full_path)
                except OSError as e:
                    logger.warning(f"Failed to delete untracked file {f}: {e}")
                    failed.append(f)

        except Exception as e:
            logger.error(f"Error discarding changes: {e}")
            failed.extend(files)

        self.invalidate_status_cache(working_dir)
        return (len(failed) == 0, failed)

    def commit(
        self,
        working_dir: str,
        message: str,
    ) -> Tuple[bool, Optional[str]]:
        """
        Create a commit.

        Args:
            working_dir: Repository directory
            message: Commit message

        Returns:
            Tuple of (success: bool, sha_or_error: Optional[str]).
            On success the second element is the commit SHA.
            On failure the second element is the error message.
        """
        try:
            # Last stop before the message becomes permanent history. Strips
            # only unambiguous generation artifacts; prose is untouched.
            from app.core.git_text import sanitize_commit_text

            args = ["commit", "-m", sanitize_commit_text(message)]
            result = self._run_git(working_dir, args, timeout=30)

            if result.returncode == 0:
                sha_result = self._run_git(working_dir, ["rev-parse", "HEAD"], timeout=5)
                commit_sha = sha_result.stdout.strip()[:12] if sha_result.returncode == 0 else None
                logger.debug(f"Created commit: {commit_sha}")
                self.invalidate_status_cache(working_dir)
                return True, commit_sha

            error_msg = result.stderr.strip() if result.stderr else "Unknown commit error"
            logger.warning(f"Failed to commit: {error_msg}")
            return False, error_msg
        except Exception as e:
            logger.error(f"Error creating commit: {e}")
            return False, str(e)

    def push(
        self,
        working_dir: str,
        remote: str = "origin",
        branch: Optional[str] = None,
        set_upstream: bool = False,
        force: bool = False,
    ) -> Tuple[bool, Optional[str]]:
        """
        Push commits to remote.

        Args:
            working_dir: Repository directory
            remote: Remote name (default: origin)
            branch: Branch name (default: current branch)
            set_upstream: Set upstream tracking
            force: Use --force-with-lease (safe force push for rebased branches)

        Returns:
            Tuple of (success: bool, error_message: Optional[str])
        """
        try:
            if remote.startswith("-"):
                return False, f"Invalid remote name: {remote}"
            if branch:
                branch = _push_branch_arg(branch)
            if set_upstream and branch:
                args = ["push", "--set-upstream", remote, branch]
            elif branch:
                args = ["push", remote, branch]
            else:
                args = ["push", remote]

            if force:
                args.insert(1, "--force-with-lease")

            result = self._run_git(working_dir, args, timeout=120)

            if result.returncode == 0:
                logger.debug(f"Pushed to {remote}/{branch or 'current branch'}")
                self.invalidate_status_cache(working_dir)
                return True, None

            error_msg = result.stderr.strip() if result.stderr else "Unknown error"
            logger.warning(f"Failed to push: {error_msg}")
            return False, error_msg
        except Exception as e:
            logger.error(f"Error pushing: {e}")
            return False, str(e)

    def stash_push(self, working_dir: str, message: str = "auto-stash") -> bool:
        """
        Stash uncommitted changes.

        Args:
            working_dir: Repository directory
            message: Stash message for identification

        Returns:
            True if changes were stashed, False if nothing to stash or error
        """
        try:
            result = self._run_git(
                working_dir, ["stash", "push", "-m", message], timeout=30
            )
            if result.returncode == 0:
                # "No local changes to save" means nothing was stashed
                output = result.stdout.strip() if result.stdout else ""
                if "No local changes" in output:
                    return False
                logger.debug(f"Stashed changes: {message}")
                return True
            return False
        except Exception as e:
            logger.error(f"Error stashing changes: {e}")
            return False

    def stash_pop(self, working_dir: str) -> Tuple[bool, Optional[str]]:
        """
        Pop the most recent stash entry.

        Returns:
            Tuple of (success, error_message)
        """
        try:
            result = self._run_git(working_dir, ["stash", "pop"], timeout=30)
            if result.returncode == 0:
                logger.debug("Popped stash successfully")
                return True, None
            error_msg = result.stderr.strip() if result.stderr else "Failed to pop stash"
            logger.warning(f"Stash pop failed: {error_msg}")
            return False, error_msg
        except Exception as e:
            logger.error(f"Error popping stash: {e}")
            return False, str(e)

    def pull(
        self,
        working_dir: str,
        remote: str = "origin",
        strategy: str = "merge",
        auto_stash: bool = False,
    ) -> Tuple[bool, Optional[str], List[str]]:
        """
        Pull from remote (fetch + merge/rebase).

        Args:
            working_dir: Repository directory
            remote: Remote name (default: origin)
            strategy: "merge" or "rebase"
            auto_stash: If True, stash uncommitted changes before pull and pop after

        Returns:
            Tuple of (success, error_message, conflicted_files)
        """
        stashed = False
        try:
            # Get current branch
            branch = self.get_current_branch(working_dir)
            if not branch:
                return False, "Cannot pull: HEAD is detached", []

            # Auto-stash uncommitted changes if requested
            if auto_stash:
                stashed = self.stash_push(working_dir, "auto-stash before pull")

            # Fetch first
            if not self.fetch(working_dir, remote):
                if stashed:
                    self.stash_pop(working_dir)
                return False, f"Failed to fetch from '{remote}'", []

            # Merge or rebase
            if strategy == "rebase":
                args = ["rebase", f"{remote}/{branch}"]
            else:
                args = ["merge", f"{remote}/{branch}"]

            result = self._run_git(working_dir, args, timeout=120)

            if result.returncode == 0:
                logger.debug(f"Pulled from {remote}/{branch} using {strategy}")
                self.invalidate_status_cache(working_dir)
                # Pop stash after successful pull
                if stashed:
                    pop_ok, pop_err = self.stash_pop(working_dir)
                    if not pop_ok:
                        return True, f"Pull succeeded but failed to restore stashed changes: {pop_err}", []
                return True, None, []

            # Check for merge conflicts
            conflicts = self._get_conflict_files(working_dir)
            if conflicts:
                # Auto-abort to keep repo clean
                if strategy == "rebase":
                    self._run_git(working_dir, ["rebase", "--abort"], timeout=10)
                else:
                    self._run_git(working_dir, ["merge", "--abort"], timeout=10)
                # Restore stashed changes after abort
                if stashed:
                    self.stash_pop(working_dir)
                return False, "Merge conflicts detected", conflicts

            # Non-conflict error — restore stash
            if stashed:
                self.stash_pop(working_dir)
            error_msg = result.stderr.strip() if result.stderr else "Unknown error"
            logger.warning(f"Pull failed: {error_msg}")
            return False, error_msg, []
        except Exception as e:
            # Restore stash on any exception
            if stashed:
                try:
                    self.stash_pop(working_dir)
                except Exception:
                    pass
            logger.error(f"Error pulling: {e}")
            return False, str(e), []

    def _get_conflict_files(self, working_dir: str) -> List[str]:
        """Get list of files with merge conflicts."""
        try:
            result = self._run_git(
                working_dir,
                ["diff", "--name-only", "--diff-filter=U"],
                timeout=10
            )
            if result.returncode == 0 and result.stdout.strip():
                return [f.strip() for f in result.stdout.strip().split("\n") if f.strip()]
            return []
        except Exception:
            return []
