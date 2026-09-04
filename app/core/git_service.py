"""
Git Service

Provides git operations for repository management, branch operations,
worktree management, and commit graph visualization.

This service wraps git CLI commands and provides a clean interface
for the API layer to interact with git repositories.

Implementation is split across mixin modules for maintainability:
- git_cache.py         — TTL-based result cache + pygit2 native reader
- git_branch_ops.py    — Branch operations (list, create, delete, checkout, etc.)
- git_worktree_ops.py  — Worktree management (list, add, remove, prune)
- git_diff_ops.py      — Diff statistics and file diffs
- git_commit_ops.py    — Commit, push, pull, stage, stash operations
"""

import logging
import os
import subprocess
from typing import List, Dict, Any, Optional, Tuple

# Re-export cache class, TTL constants, and native reader for backward compatibility
from app.core.git_cache import (  # noqa: F401
    _GitResultCache,
    _git_cache,
    _native_reader,
    STATUS_CACHE_TTL as _STATUS_CACHE_TTL,
    DIFF_STATS_CACHE_TTL as _DIFF_STATS_CACHE_TTL,
    DEFAULT_BRANCH_CACHE_TTL as _DEFAULT_BRANCH_CACHE_TTL,
    AHEAD_BEHIND_CACHE_TTL as _AHEAD_BEHIND_CACHE_TTL,
)

# Re-export mixin classes for backward compatibility
from app.core.git_branch_ops import GitBranchMixin  # noqa: F401
from app.core.git_worktree_ops import GitWorktreeMixin  # noqa: F401
from app.core.git_diff_ops import GitDiffMixin  # noqa: F401
from app.core.git_commit_ops import GitCommitMixin  # noqa: F401

logger = logging.getLogger(__name__)


class GitService(
    GitBranchMixin,
    GitWorktreeMixin,
    GitDiffMixin,
    GitCommitMixin,
):
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
        check: bool = False,
        env: Optional[Dict[str, str]] = None,
    ) -> subprocess.CompletedProcess:
        """
        Run a git command and return the result.

        Args:
            working_dir: Directory to run the command in
            args: Git command arguments (without 'git' prefix)
            timeout: Command timeout in seconds
            check: If True, raise CalledProcessError on non-zero exit
            env: Optional environment mapping for the subprocess. ``None`` (the
                default) inherits the parent process environment exactly as before,
                so existing callers are unaffected.

        Returns:
            CompletedProcess with returncode, stdout, stderr
        """
        # Never let git (or a credential helper it spawns) wait on a terminal
        # prompt: the server has no TTY, so a fetch/clone that needs
        # credentials would otherwise sit until the timeout. The caller's env
        # wins when it sets the variable explicitly.
        run_env = dict(env if env is not None else os.environ)
        run_env.setdefault("GIT_TERMINAL_PROMPT", "0")
        try:
            return subprocess.run(
                ["git"] + args,
                cwd=working_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=check,
                env=run_env,
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
            Tuple of (success, error_message). A clone URL that is rejected by the
            safety guards fails closed with (False, reason) before any subprocess
            runs — matching the return-False guard style in git_branch_ops.py.
        """
        # Reject flag injection: a url beginning with '-' would be parsed by git
        # as an option. Reject remote-helper schemes (ext::/file::) which can run
        # arbitrary local commands. Fail closed with the tuple contract rather than
        # raising, so callers surface a clean error instead of a 500.
        stripped = clone_url.strip()
        if stripped.startswith("-"):
            logger.warning("Refusing clone URL that looks like an option: %s", clone_url[:50])
            return False, "Invalid clone URL (looks like a command-line option)"
        lowered = stripped.lower()
        if lowered.startswith("ext::") or lowered.startswith("file::"):
            logger.warning("Refusing unsafe clone URL scheme: %s", clone_url[:50])
            return False, "Refusing unsafe clone URL scheme (ext:: / file::)"

        # Force-disable git's interactive credential prompt so a clone of a
        # private/credential-requiring remote fails fast instead of blocking on a
        # username/password prompt until the timeout. Merged onto (never replacing)
        # the inherited environment so existing git config/PATH survive.
        clone_env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}

        try:
            # `--` terminates option parsing so the url is always treated as a
            # positional argument even if a future value slips past the guard.
            result = self._run_git(
                working_dir,
                ["clone", "--", clone_url, "."],
                timeout=timeout,
                env=clone_env,
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

    def fetch_refspec(self, working_dir: str, refspec: str, remote: str = "origin") -> bool:
        """Fetch a single refspec (no prune) — used to restore a deleted PR branch."""
        try:
            result = self._run_git(
                working_dir,
                ["fetch", remote, refspec],
                timeout=60,
            )
            if result.returncode == 0:
                logger.debug("Fetched %s from %s", refspec, remote)
                return True
            logger.warning("Fetch refspec %s failed: %s", refspec, result.stderr)
            return False
        except Exception as e:
            logger.error("Fetch refspec error: %s", e)
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
            "differs_from_base": False,
            "unpushed": None,
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
                if ab_cached is not None and len(ab_cached) >= 3:
                    status["ahead"], status["behind"], status["differs_from_base"] = ab_cached[:3]
                elif ab_cached is not None:
                    # Legacy 2-tuple cached before differs_from_base existed.
                    status["ahead"], status["behind"] = ab_cached[:2]
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
                            # Only commits with net content over the base count as
                            # shippable: a three-dot (base...HEAD) diff is empty when
                            # commits cancel out or the branch is already merged.
                            # `git diff --quiet` exits 1 on differences, 0 if none;
                            # any other code (e.g. missing ref) → assume content so
                            # we don't hide a control that should show.
                            if status["ahead"] > 0:
                                diff_check = self._run_git(
                                    working_dir,
                                    ["diff", "--quiet", f"{ref}...HEAD"],
                                    timeout=5
                                )
                                status["differs_from_base"] = diff_check.returncode != 0
                            _git_cache.put(
                                ab_cache_key,
                                (status["ahead"], status["behind"], status["differs_from_base"])
                            )
                            break

                # Unpushed vs origin/<branch>. None when that remote ref does not
                # exist (branch never pushed) — see git_native.get_status.
                own_remote = f"origin/{status['current_branch']}"
                check = self._run_git(
                    working_dir,
                    ["rev-parse", "--verify", "--quiet", own_remote],
                    timeout=5,
                )
                if check.returncode == 0:
                    ab = self._run_git(
                        working_dir,
                        ["rev-list", "--left-right", "--count",
                         f"HEAD...{own_remote}"],
                        timeout=5,
                    )
                    if ab.returncode == 0:
                        parts = ab.stdout.strip().split()
                        if len(parts) >= 1:
                            try:
                                status["unpushed"] = int(parts[0])
                            except ValueError:
                                status["unpushed"] = None

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


# Global instance
git_service = GitService()
