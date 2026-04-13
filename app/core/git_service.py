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
import threading
import time
from typing import List, Dict, Any, Optional, Tuple

logger = logging.getLogger(__name__)

# Native git reader (pygit2) — falls back to CLI if unavailable
try:
    from app.core.git_native import GitNativeReader
    _native_reader: "GitNativeReader | None" = GitNativeReader()
    logger.info("Git read operations: using pygit2 (native)")
except ImportError:
    _native_reader = None
    logger.info("Git read operations: using CLI subprocess (pygit2 not available)")

# ---------------------------------------------------------------------------
# Lightweight TTL cache for expensive git operations
# ---------------------------------------------------------------------------
_STATUS_CACHE_TTL = 2.0        # seconds — match active poll cadence for fresh UI
_DIFF_STATS_CACHE_TTL = 2.0    # seconds
_DEFAULT_BRANCH_CACHE_TTL = 300.0  # 5 minutes — rarely changes
_AHEAD_BEHIND_CACHE_TTL = 30.0  # seconds — only changes on commit/push/fetch

class _GitResultCache:
    """Thread-safe per-key TTL cache for git results."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # key -> (result, monotonic_timestamp)
        self._data: Dict[str, tuple] = {}

    def get(self, key: str, ttl: float) -> Optional[Any]:
        with self._lock:
            entry = self._data.get(key)
            if entry and (time.monotonic() - entry[1]) < ttl:
                return entry[0]
            return None

    def put(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = (value, time.monotonic())

    def invalidate(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)

    def invalidate_prefix(self, prefix: str) -> None:
        """Invalidate all keys starting with *prefix*."""
        with self._lock:
            to_remove = [k for k in self._data if k.startswith(prefix)]
            for k in to_remove:
                del self._data[k]

    def purge_expired(self, max_ttl: float = 300.0) -> int:
        """Remove all entries older than max_ttl seconds. Returns count removed."""
        now = time.monotonic()
        with self._lock:
            expired = [k for k, (_, ts) in self._data.items() if (now - ts) > max_ttl]
            for k in expired:
                del self._data[k]
            return len(expired)

_git_cache = _GitResultCache()


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
                encoding="utf-8",
                errors="replace",
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
        """Check if the working directory is a git repository root.

        Uses --show-toplevel to verify that the directory IS the repo root,
        not merely a subdirectory inside a parent repo.  Without this check,
        a project directory sitting inside another repo (e.g. ai-shuffle)
        would incorrectly report as a git repo and show the parent's diff stats.
        """
        if _native_reader is not None:
            try:
                return _native_reader.is_git_repo(working_dir)
            except Exception as e:
                logger.warning(f"pygit2 is_git_repo failed, CLI fallback: {e}")
        return self._is_git_repo_cli(working_dir)

    def _is_git_repo_cli(self, working_dir: str) -> bool:
        try:
            result = self._run_git(working_dir, ["rev-parse", "--show-toplevel"], timeout=5)
            if result.returncode != 0:
                return False
            # Compare resolved paths so symlinks / trailing slashes don't cause mismatches
            toplevel = os.path.realpath(result.stdout.strip())
            target = os.path.realpath(working_dir)
            return toplevel == target
        except (subprocess.SubprocessError, OSError) as e:
            logger.warning(f"is_git_repo check failed for '{working_dir}': {e}")
            return False

    def init_repo(self, working_dir: str, default_branch: str = "main") -> bool:
        """Initialize a new git repository.

        Args:
            working_dir: Directory to initialize as a git repo
            default_branch: Name for the initial branch (default: main)

        Returns:
            True if successful, False otherwise
        """
        try:
            result = self._run_git(
                working_dir,
                ["init", "--initial-branch", default_branch],
                timeout=10
            )
            if result.returncode == 0:
                logger.debug(f"Initialized git repository at {working_dir}")
                if _native_reader is not None:
                    _native_reader.invalidate_repo(working_dir)
                return True
            logger.warning(f"Failed to init repo: {result.stderr}")
            return False
        except Exception as e:
            logger.error(f"Error initializing git repository: {e}")
            return False

    def clone_repo(self, working_dir: str, clone_url: str, timeout: int = 120) -> Tuple[bool, Optional[str]]:
        """Clone a remote repository into the working directory.

        The working directory must already exist and be empty (or nearly empty).
        Clones with `git clone <url> .` to place contents directly in working_dir.

        Args:
            working_dir: Target directory to clone into (must exist)
            clone_url: Remote repository URL (HTTPS or SSH)
            timeout: Command timeout in seconds (default 120 for network ops)

        Returns:
            Tuple of (success, error_message)
        """
        try:
            result = self._run_git(
                working_dir,
                ["clone", clone_url, "."],
                timeout=timeout
            )
            if result.returncode == 0:
                logger.debug(f"Cloned repository from {clone_url} into {working_dir}")
                if _native_reader is not None:
                    _native_reader.invalidate_repo(working_dir)
                return True, None
            error = result.stderr.strip() or "Unknown error"
            logger.warning(f"Failed to clone repo: {error}")
            return False, error
        except subprocess.TimeoutExpired:
            return False, f"Clone timed out after {timeout}s — the repository may be too large"
        except Exception as e:
            logger.error(f"Error cloning repository: {e}")
            return False, str(e)

    def get_remote_url(self, working_dir: str) -> Optional[str]:
        """Get the remote URL for origin."""
        if _native_reader is not None:
            try:
                return _native_reader.get_remote_url(working_dir)
            except Exception as e:
                logger.warning(f"pygit2 get_remote_url failed, CLI fallback: {e}")
        return self._get_remote_url_cli(working_dir)

    def _get_remote_url_cli(self, working_dir: str) -> Optional[str]:
        try:
            result = self._run_git(working_dir, ["remote", "get-url", "origin"], timeout=5)
            if result.returncode == 0:
                return result.stdout.strip()
            return None
        except (subprocess.SubprocessError, OSError) as e:
            logger.warning(f"get_remote_url failed for '{working_dir}': {e}")
            return None

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
        cached = _git_cache.get(cache_key, _DEFAULT_BRANCH_CACHE_TTL)
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
                logger.debug(f"Fetched from {remote}")
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
                logger.debug(f"Removed worktree at {path}")
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
        if _native_reader is not None:
            try:
                return _native_reader.get_commit_graph(working_dir, limit, branch)
            except Exception as e:
                logger.warning(f"pygit2 get_commit_graph failed, CLI fallback: {e}")
        return self._get_commit_graph_cli(working_dir, limit, branch)

    def _get_commit_graph_cli(
        self,
        working_dir: str,
        limit: int = 100,
        branch: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        try:
            # Format: SHA|short SHA|message|author|email|timestamp|parents|refs
            format_str = "%H|%h|%s|%an|%ae|%aI|%P|%D"
            args = [
                "log",
                f"--format={format_str}",
                f"-{limit}",
            ]
            if not branch:
                args.append("--all")
            else:
                # Use refs/heads/ prefix to prevent flag injection from branch names starting with -
                safe_branch = f"refs/heads/{branch}" if branch.startswith("-") else branch
                args.append(safe_branch)

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

    def get_diff_stats(
        self,
        working_dir: str,
        include_unstaged: bool = True
    ) -> Dict[str, Any]:
        """
        Get diff statistics (lines added/removed).
        Results are cached for a short TTL to coalesce rapid polls.

        Args:
            working_dir: Repository directory
            include_unstaged: If True, include both staged and unstaged changes

        Returns:
            Dictionary with:
            - additions: Total lines added
            - deletions: Total lines deleted
            - files_changed: Number of files changed
        """
        cache_key = f"diff_stats:{working_dir}:{include_unstaged}"
        cached = _git_cache.get(cache_key, _DIFF_STATS_CACHE_TTL)
        if cached is not None:
            return cached

        # Try native reader
        if _native_reader is not None:
            try:
                result = _native_reader.get_diff_stats(working_dir, include_unstaged)
                _git_cache.put(cache_key, result)
                return result
            except Exception as e:
                logger.warning(f"pygit2 get_diff_stats failed, CLI fallback: {e}")

        # CLI fallback
        return self._get_diff_stats_cli(working_dir, include_unstaged)

    def _get_diff_stats_cli(
        self,
        working_dir: str,
        include_unstaged: bool = True
    ) -> Dict[str, Any]:
        cache_key = f"diff_stats:{working_dir}:{include_unstaged}"
        try:
            total_additions = 0
            total_deletions = 0
            files = set()

            # Get staged diff stats
            result = self._run_git(working_dir, ["diff", "--staged", "--numstat"], timeout=10)
            if result.returncode == 0:
                for line in result.stdout.strip().split("\n"):
                    if not line:
                        continue
                    parts = line.split("\t")
                    if len(parts) >= 3:
                        added = int(parts[0]) if parts[0] != '-' else 0
                        deleted = int(parts[1]) if parts[1] != '-' else 0
                        total_additions += added
                        total_deletions += deleted
                        files.add(parts[2])

            # Get unstaged diff stats
            if include_unstaged:
                result = self._run_git(working_dir, ["diff", "--numstat"], timeout=10)
                if result.returncode == 0:
                    for line in result.stdout.strip().split("\n"):
                        if not line:
                            continue
                        parts = line.split("\t")
                        if len(parts) >= 3:
                            added = int(parts[0]) if parts[0] != '-' else 0
                            deleted = int(parts[1]) if parts[1] != '-' else 0
                            total_additions += added
                            total_deletions += deleted
                            files.add(parts[2])

            result_data = {
                "additions": total_additions,
                "deletions": total_deletions,
                "files_changed": len(files)
            }
            _git_cache.put(cache_key, result_data)
            return result_data
        except Exception as e:
            logger.error(f"Error getting diff stats: {e}")
            return {"additions": 0, "deletions": 0, "files_changed": 0}

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
            file_statuses: Map of file path → category ('modified', 'staged', 'untracked').
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
                full_path = os.path.realpath(os.path.join(working_dir, f))
                if not full_path.startswith(real_working_dir + os.sep) and full_path != real_working_dir:
                    logger.warning(f"Path traversal attempt blocked: {f}")
                    failed.append(f)
                    continue
                try:
                    if os.path.isdir(full_path):
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

    def get_file_diff(
        self,
        working_dir: str,
        file_path: str,
        staged: bool = False,
        max_chars: int = 50000,
    ) -> Dict[str, Any]:
        """
        Get unified diff for a single file.

        For tracked files, runs git diff. For untracked files, reads
        the file and synthesizes an all-additions diff.

        Args:
            working_dir: Repository directory
            file_path: Path to the file (relative to repo root)
            staged: If True, show staged diff (index vs HEAD)
            max_chars: Maximum diff chars before truncation

        Returns:
            Dictionary with: file, diff, is_binary, truncated
        """
        if _native_reader is not None:
            try:
                return _native_reader.get_file_diff(working_dir, file_path, staged, max_chars)
            except Exception as e:
                logger.warning(f"pygit2 get_file_diff failed, CLI fallback: {e}")
        return self._get_file_diff_cli(working_dir, file_path, staged, max_chars)

    def _get_file_diff_cli(
        self,
        working_dir: str,
        file_path: str,
        staged: bool = False,
        max_chars: int = 50000,
    ) -> Dict[str, Any]:
        result_data: Dict[str, Any] = {
            "file": file_path,
            "diff": "",
            "is_binary": False,
            "truncated": False,
        }

        try:
            # Check if file is untracked
            status_result = self._run_git(
                working_dir,
                ["status", "--porcelain=v1", "--", file_path],
                timeout=5,
            )
            is_untracked = False
            if status_result.returncode == 0:
                for line in status_result.stdout.split("\n"):
                    if line and len(line) >= 3 and line[0:2] == "??":
                        is_untracked = True
                        break

            if is_untracked:
                # Synthesize diff for untracked file
                full_path = os.path.realpath(os.path.join(working_dir, file_path))
                real_wd = os.path.realpath(working_dir)
                if not full_path.startswith(real_wd + os.sep) and full_path != real_wd:
                    result_data["diff"] = "Error: path traversal blocked"
                    return result_data

                # Check if binary
                numstat = self._run_git(
                    working_dir,
                    ["diff", "--no-index", "--numstat", "/dev/null", file_path],
                    timeout=5,
                )
                if numstat.returncode in (0, 1) and numstat.stdout.strip():
                    parts = numstat.stdout.strip().split("\t")
                    if len(parts) >= 2 and parts[0] == "-" and parts[1] == "-":
                        result_data["is_binary"] = True
                        result_data["diff"] = "Binary file (not displayable)"
                        return result_data

                # Use git diff --no-index for proper unified diff format
                diff_result = self._run_git(
                    working_dir,
                    ["diff", "--no-index", "--", "/dev/null", file_path],
                    timeout=15,
                )
                # --no-index returns exit code 1 when files differ (normal)
                if diff_result.returncode in (0, 1) and diff_result.stdout:
                    diff_text = diff_result.stdout
                    if len(diff_text) > max_chars:
                        diff_text = diff_text[:max_chars] + "\n\n[... diff truncated ...]"
                        result_data["truncated"] = True
                    result_data["diff"] = diff_text
                return result_data

            # Check if binary (tracked file)
            numstat_args = ["diff", "--numstat"]
            if staged:
                numstat_args.append("--staged")
            numstat_args.extend(["--", file_path])
            numstat = self._run_git(working_dir, numstat_args, timeout=5)
            if numstat.returncode == 0 and numstat.stdout.strip():
                parts = numstat.stdout.strip().split("\t")
                if len(parts) >= 2 and parts[0] == "-" and parts[1] == "-":
                    result_data["is_binary"] = True
                    result_data["diff"] = "Binary file (not displayable)"
                    return result_data

            # Get the unified diff
            diff_args = ["diff"]
            if staged:
                diff_args.append("--staged")
            diff_args.extend(["--", file_path])

            diff_result = self._run_git(working_dir, diff_args, timeout=15)
            if diff_result.returncode == 0:
                diff_text = diff_result.stdout
                if len(diff_text) > max_chars:
                    diff_text = diff_text[:max_chars] + "\n\n[... diff truncated ...]"
                    result_data["truncated"] = True
                result_data["diff"] = diff_text

        except Exception as e:
            logger.error(f"Error getting file diff for {file_path}: {e}")
            result_data["diff"] = f"Error: {e}"

        return result_data

    def get_per_file_diff_stats(self, working_dir: str) -> List[Dict[str, Any]]:
        """
        Get per-file diff statistics (additions/deletions per file).

        Returns separate entries for staged and unstaged changes.
        Cached with the same TTL as aggregate diff stats.

        Args:
            working_dir: Repository directory

        Returns:
            List of dicts with: file, additions, deletions, is_binary, staged
        """
        cache_key = f"per_file_diff:{working_dir}"
        cached = _git_cache.get(cache_key, _DIFF_STATS_CACHE_TTL)
        if cached is not None:
            return cached

        # Try native reader
        if _native_reader is not None:
            try:
                result = _native_reader.get_per_file_diff_stats(working_dir)
                _git_cache.put(cache_key, result)
                return result
            except Exception as e:
                logger.warning(f"pygit2 get_per_file_diff_stats failed, CLI fallback: {e}")

        # CLI fallback
        return self._get_per_file_diff_stats_cli(working_dir)

    def _get_per_file_diff_stats_cli(self, working_dir: str) -> List[Dict[str, Any]]:
        cache_key = f"per_file_diff:{working_dir}"
        files: List[Dict[str, Any]] = []

        try:
            # Staged changes
            result = self._run_git(
                working_dir, ["diff", "--staged", "--numstat"], timeout=10
            )
            if result.returncode == 0:
                for line in result.stdout.strip().split("\n"):
                    if not line:
                        continue
                    parts = line.split("\t")
                    if len(parts) >= 3:
                        is_binary = parts[0] == "-" and parts[1] == "-"
                        files.append({
                            "file": parts[2],
                            "additions": 0 if is_binary else int(parts[0]),
                            "deletions": 0 if is_binary else int(parts[1]),
                            "is_binary": is_binary,
                            "staged": True,
                        })

            # Unstaged changes
            result = self._run_git(
                working_dir, ["diff", "--numstat"], timeout=10
            )
            if result.returncode == 0:
                for line in result.stdout.strip().split("\n"):
                    if not line:
                        continue
                    parts = line.split("\t")
                    if len(parts) >= 3:
                        is_binary = parts[0] == "-" and parts[1] == "-"
                        files.append({
                            "file": parts[2],
                            "additions": 0 if is_binary else int(parts[0]),
                            "deletions": 0 if is_binary else int(parts[1]),
                            "is_binary": is_binary,
                            "staged": False,
                        })

            _git_cache.put(cache_key, files)
        except Exception as e:
            logger.error(f"Error getting per-file diff stats: {e}")

        return files

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
            args = ["commit", "-m", message]
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

    def invalidate_status_cache(self, working_dir: str) -> None:
        """Invalidate cached status/diff for a working directory.

        Call after mutations (commit, checkout, stage, push, pull, etc.)
        so the next poll gets fresh data.
        """
        _git_cache.invalidate(f"status:{working_dir}")
        _git_cache.invalidate_prefix(f"diff_stats:{working_dir}:")
        _git_cache.invalidate_prefix(f"per_file_diff:{working_dir}")
        _git_cache.invalidate_prefix(f"ahead_behind:{working_dir}:")

    def get_status(self, working_dir: str) -> Dict[str, Any]:
        """
        Get repository status.

        Results are cached for a short TTL to coalesce rapid polls.

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
        cache_key = f"status:{working_dir}"
        cached = _git_cache.get(cache_key, _STATUS_CACHE_TTL)
        if cached is not None:
            return cached

        # Try native reader
        if _native_reader is not None:
            try:
                result = _native_reader.get_status(working_dir)
                _git_cache.put(cache_key, result)
                return result
            except Exception as e:
                logger.warning(f"pygit2 get_status failed, CLI fallback: {e}")

        # CLI fallback
        return self._get_status_cli(working_dir)

    def _get_status_cli(self, working_dir: str) -> Dict[str, Any]:
        cache_key = f"status:{working_dir}"
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

        # Use rev-parse --git-dir to check if it's a repo (avoids separate is_git_repo call)
        try:
            check = self._run_git(working_dir, ["rev-parse", "--git-dir"], timeout=5)
            if check.returncode != 0:
                return status
        except (subprocess.SubprocessError, OSError):
            return status

        status["is_git_repo"] = True

        try:
            # Combine branch + HEAD + remote URL in fewer calls
            # Get branch and HEAD in one rev-parse call (two queries)
            result = self._run_git(
                working_dir,
                ["rev-parse", "--abbrev-ref", "HEAD"],
                timeout=5
            )
            if result.returncode == 0:
                branch = result.stdout.strip()
                if branch == "HEAD":
                    status["is_detached"] = True
                else:
                    status["current_branch"] = branch

            result = self._run_git(working_dir, ["rev-parse", "HEAD"], timeout=5)
            if result.returncode == 0:
                status["head_commit"] = result.stdout.strip()[:12]

            # Get remote URL (single lightweight call)
            result = self._run_git(working_dir, ["remote", "get-url", "origin"], timeout=5)
            if result.returncode == 0:
                status["remote_url"] = result.stdout.strip()

            # Get ahead/behind relative to the default branch (main/master).
            # This is more useful for the PR workflow than comparing against
            # the upstream tracking branch (which would be 0 after pushing).
            # Cached separately with a longer TTL because rev-list walks the
            # commit graph (O(n) in divergence) and only changes on
            # commit/push/fetch — all of which call invalidate_status_cache.
            if status["current_branch"]:
                ab_cache_key = f"ahead_behind:{working_dir}:{status['current_branch']}"
                ab_cached = _git_cache.get(ab_cache_key, _AHEAD_BEHIND_CACHE_TTL)
                if ab_cached is not None:
                    status["ahead"], status["behind"] = ab_cached
                else:
                    default_branch = self.get_default_branch(working_dir)
                    for ref in [f"origin/{default_branch}", default_branch]:
                        result = self._run_git(
                            working_dir,
                            ["rev-list", "--left-right", "--count",
                             f"{status['current_branch']}...{ref}"],
                            timeout=5
                        )
                        if result.returncode == 0:
                            parts = result.stdout.strip().split()
                            if len(parts) >= 2:
                                status["ahead"] = int(parts[0])
                                status["behind"] = int(parts[1])
                            _git_cache.put(ab_cache_key, (status["ahead"], status["behind"]))
                            break

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

        _git_cache.put(cache_key, status)
        return status

    def get_diff_text(
        self,
        working_dir: str,
        include_unstaged: bool = True,
        max_chars: int = 8000
    ) -> str:
        """
        Get the actual diff text for AI commit message generation.

        Args:
            working_dir: Repository directory
            include_unstaged: Whether to include unstaged changes
            max_chars: Maximum characters to return (truncates if larger)

        Returns:
            Combined diff text, truncated to max_chars
        """
        if _native_reader is not None:
            try:
                return _native_reader.get_diff_text(working_dir, include_unstaged, max_chars)
            except Exception as e:
                logger.warning(f"pygit2 get_diff_text failed, CLI fallback: {e}")
        return self._get_diff_text_cli(working_dir, include_unstaged, max_chars)

    def _get_diff_text_cli(
        self,
        working_dir: str,
        include_unstaged: bool = True,
        max_chars: int = 8000
    ) -> str:
        parts = []

        # Staged changes
        result = self._run_git(working_dir, ["diff", "--staged"], timeout=15)
        if result.returncode == 0 and result.stdout and result.stdout.strip():
            parts.append(result.stdout.strip())

        # Unstaged changes
        if include_unstaged:
            result = self._run_git(working_dir, ["diff"], timeout=15)
            if result.returncode == 0 and result.stdout and result.stdout.strip():
                parts.append(result.stdout.strip())

        diff_text = "\n".join(parts)

        if len(diff_text) > max_chars:
            diff_text = diff_text[:max_chars] + "\n\n[... diff truncated ...]"

        return diff_text

    def get_branch_log(
        self,
        working_dir: str,
        base_branch: str = "main",
        max_commits: int = 50
    ) -> tuple:
        """
        Get commit log and diff stat between base branch and HEAD.

        Args:
            working_dir: Repository directory
            base_branch: Base branch to compare against
            max_commits: Maximum number of commits to include

        Returns:
            Tuple of (commit_log, diff_stat) strings
        """
        if _native_reader is not None:
            try:
                return _native_reader.get_branch_log(working_dir, base_branch, max_commits)
            except Exception as e:
                logger.warning(f"pygit2 get_branch_log failed, CLI fallback: {e}")
        return self._get_branch_log_cli(working_dir, base_branch, max_commits)

    def _get_branch_log_cli(
        self,
        working_dir: str,
        base_branch: str = "main",
        max_commits: int = 50
    ) -> tuple:
        commit_log = ""
        diff_stat = ""

        # Get commit messages
        result = self._run_git(
            working_dir,
            ["log", f"{base_branch}..HEAD", "--oneline", f"-{max_commits}"],
            timeout=10
        )
        if result.returncode == 0:
            commit_log = result.stdout.strip()

        # Get diff stat summary
        result = self._run_git(
            working_dir,
            ["diff", f"{base_branch}..HEAD", "--stat"],
            timeout=10
        )
        if result.returncode == 0:
            diff_stat = result.stdout.strip()

        return (commit_log, diff_stat)

    # =========================================================================
    # Efficient Ahead/Behind Methods
    # =========================================================================

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


# Global instance
git_service = GitService()
