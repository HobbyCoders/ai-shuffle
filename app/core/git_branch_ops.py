"""
Git Branch Operations Mixin

Provides branch-related git operations: list, create, delete, checkout,
get current/default branch, ahead/behind counts, and remote HEAD checks.
"""

import logging
import subprocess
from typing import Any, Dict, List, Optional, Tuple

from app.core.git_cache import (
    _git_cache,
    _native_reader,
    AHEAD_BEHIND_CACHE_TTL,
    DEFAULT_BRANCH_CACHE_TTL,
)

logger = logging.getLogger(__name__)


class GitBranchMixin:
    """Mixin providing branch operations for GitService."""

    def get_current_branch(self, working_dir: str) -> Optional[str]:
        """Get the current branch name."""
        if _native_reader is not None:
            try:
                return _native_reader.get_current_branch(working_dir)
            except Exception as e:
                logger.warning(f"pygit2 get_current_branch failed, CLI fallback: {e}")
        return self._get_current_branch_cli(working_dir)

    def _get_current_branch_cli(self, working_dir: str) -> Optional[str]:
        try:
            result = self._run_git(working_dir, ["rev-parse", "--abbrev-ref", "HEAD"], timeout=5)
            if result.returncode == 0:
                branch = result.stdout.strip()
                return branch if branch != "HEAD" else None
            return None
        except (subprocess.SubprocessError, OSError) as e:
            logger.warning(f"get_current_branch failed for '{working_dir}': {e}")
            return None

    def get_default_branch(self, working_dir: str) -> str:
        """
        Get the default branch name (usually main or master).
        Checks remote HEAD first, falls back to common names.
        Results are cached for 5 minutes per repo.
        """
        cache_key = f"default_branch:{working_dir}"
        cached = _git_cache.get(cache_key, DEFAULT_BRANCH_CACHE_TTL)
        if cached is not None:
            return cached

        # Try native reader
        if _native_reader is not None:
            try:
                result = _native_reader.get_default_branch(working_dir)
                _git_cache.put(cache_key, result)
                return result
            except Exception as e:
                logger.warning(f"pygit2 get_default_branch failed, CLI fallback: {e}")

        # CLI fallback
        return self._get_default_branch_cli(working_dir)

    def _get_default_branch_cli(self, working_dir: str) -> str:
        cache_key = f"default_branch:{working_dir}"
        try:
            # Try to get remote HEAD
            result = self._run_git(
                working_dir,
                ["symbolic-ref", "refs/remotes/origin/HEAD", "--short"],
                timeout=5
            )
            if result.returncode == 0:
                branch = result.stdout.strip().replace("origin/", "")
                _git_cache.put(cache_key, branch)
                return branch

            # Fallback: check if main or master exists
            for branch in ["main", "master"]:
                result = self._run_git(
                    working_dir,
                    ["rev-parse", "--verify", f"refs/heads/{branch}"],
                    timeout=5
                )
                if result.returncode == 0:
                    _git_cache.put(cache_key, branch)
                    return branch

            _git_cache.put(cache_key, "main")
            return "main"  # Default fallback
        except (subprocess.SubprocessError, OSError) as e:
            logger.warning(f"get_default_branch failed for '{working_dir}': {e}")
            return "main"

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
        if _native_reader is not None:
            try:
                return _native_reader.list_branches(working_dir, include_remote)
            except Exception as e:
                logger.warning(f"pygit2 list_branches failed, CLI fallback: {e}")
        return self._list_branches_cli(working_dir, include_remote)

    def _list_branches_cli(
        self,
        working_dir: str,
        include_remote: bool = True
    ) -> List[Dict[str, Any]]:
        try:
            # Use for-each-ref for structured output.
            # Include %(refname) to reliably determine local vs remote.
            # IMPORTANT: %(subject) is placed LAST because commit messages
            # can contain the | delimiter. Using maxsplit ensures the subject
            # (which may contain pipes) is captured as a single trailing field.
            format_str = "%(refname)|%(refname:short)|%(objectname:short)|%(HEAD)|%(upstream:short)|%(upstream:track)|%(subject)"
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

                # Split into exactly 7 parts (6 splits). The 7th part
                # (subject) captures everything after the 6th pipe,
                # including any pipes within the commit message itself.
                parts = line.split("|", 6)
                if len(parts) < 6:
                    continue

                refname = parts[0]
                name = parts[1]
                commit = parts[2]
                is_current = parts[3] == "*"
                upstream = parts[4]
                track_info = parts[5]
                message = parts[6] if len(parts) > 6 else ""

                # Skip HEAD reference and bare remote names
                if name == "origin/HEAD" or name == "origin":
                    continue

                # Determine local vs remote from the full ref path
                is_remote = refname.startswith("refs/remotes/")

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

                # Determine the effective upstream for pull/push operations.
                # If a branch's configured upstream points to a DIFFERENT branch
                # (e.g., feature tracking origin/main), use the branch's own
                # remote counterpart (origin/feature) for ahead/behind instead.
                # This prevents "Pull 1" showing on feature when main gets a
                # new commit — the pull button should only reflect changes to
                # the branch's own remote counterpart.
                effective_upstream = upstream if upstream else None
                if not is_remote and upstream and not upstream.endswith(f"/{name}"):
                    # Upstream points to a different branch (e.g., origin/main
                    # for a branch named "feature"). Extract the remote name
                    # and check if the branch's own remote counterpart exists.
                    remote_name = upstream.split("/")[0] if "/" in upstream else "origin"
                    own_remote_ref = f"{remote_name}/{name}"
                    check = self._run_git(
                        working_dir,
                        ["rev-parse", "--verify", "--quiet", f"refs/remotes/{own_remote_ref}"],
                        timeout=3
                    )
                    if check.returncode == 0:
                        # Own remote counterpart exists — compute ahead/behind
                        # against it instead of the configured upstream.
                        effective_upstream = own_remote_ref
                        ab_result = self._run_git(
                            working_dir,
                            ["rev-list", "--left-right", "--count",
                             f"{name}...{own_remote_ref}"],
                            timeout=5
                        )
                        if ab_result.returncode == 0:
                            ab_parts = ab_result.stdout.strip().split()
                            if len(ab_parts) >= 2:
                                ahead = int(ab_parts[0])
                                behind = int(ab_parts[1])
                    else:
                        # No remote counterpart — branch hasn't been pushed yet.
                        # Reset ahead/behind to 0 since there's nothing to
                        # pull from; the upstream (e.g., origin/main) comparison
                        # is misleading for pull/push UI.
                        ahead = 0
                        behind = 0
                        effective_upstream = None

                branches.append({
                    "name": name,
                    "is_current": is_current,
                    "is_remote": is_remote,
                    "commit": commit,
                    "commit_message": message[:100],
                    "upstream": effective_upstream,
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
            args = ["branch", "--", name]
            if start_point:
                args.append(start_point)

            result = self._run_git(working_dir, args, timeout=10)
            if result.returncode == 0:
                logger.debug(f"Created branch: {name}")
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
            result = self._run_git(working_dir, ["branch", flag, "--", name], timeout=10)
            if result.returncode == 0:
                logger.debug(f"Deleted branch: {name}")
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
            # Use refs/heads/ prefix to prevent flag injection from ref names starting with -
            safe_ref = f"refs/heads/{ref}" if ref.startswith("-") else ref
            result = self._run_git(working_dir, ["checkout", safe_ref], timeout=30)
            if result.returncode == 0:
                logger.debug(f"Checked out: {ref}")
                self.invalidate_status_cache(working_dir)
                return True

            logger.warning(f"Failed to checkout {ref}: {result.stderr}")
            return False
        except Exception as e:
            logger.error(f"Error checking out {ref}: {e}")
            return False

    def get_ahead_behind_counts(self, working_dir: str) -> Tuple[int, int]:
        """Get ahead/behind counts relative to upstream tracking branch.

        Uses 'git rev-list --count --left-right' for efficient computation
        without running full git status.

        Returns:
            Tuple of (ahead, behind) counts. (0, 0) on error.
        """
        if _native_reader is not None:
            try:
                return _native_reader.get_ahead_behind_counts(working_dir)
            except Exception as e:
                logger.warning(f"pygit2 get_ahead_behind_counts failed, CLI fallback: {e}")
        return self._get_ahead_behind_counts_cli(working_dir)

    def _get_ahead_behind_counts_cli(self, working_dir: str) -> Tuple[int, int]:
        try:
            result = self._run_git(
                working_dir,
                ["rev-list", "--count", "--left-right", "@{u}...HEAD"],
                timeout=5
            )
            if result.returncode == 0:
                parts = result.stdout.strip().split()
                if len(parts) >= 2:
                    return (int(parts[1]), int(parts[0]))
        except (subprocess.SubprocessError, OSError, ValueError):
            pass
        return (0, 0)

    def check_remote_head(self, working_dir: str, remote: str = "origin") -> Optional[str]:
        """Check the remote HEAD SHA without performing a fetch.

        Uses 'git ls-remote' which is a lightweight network operation
        (~100ms) that only reads the remote ref advertisement.

        Returns:
            The SHA-1 of the remote HEAD, or None on error.
        """
        try:
            result = self._run_git(
                working_dir,
                ["ls-remote", "--head", remote, "HEAD"],
                timeout=10
            )
            if result.returncode == 0 and result.stdout.strip():
                # Output format: "<sha>\tHEAD"
                return result.stdout.strip().split()[0]
        except (subprocess.SubprocessError, OSError):
            pass
        return None
