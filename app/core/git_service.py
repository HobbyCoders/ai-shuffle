"""
Git Service

Provides git operations for repository management, branch operations,
worktree management, and commit graph visualization.

This service wraps git CLI commands and provides a clean interface
for the API layer to interact with git repositories. Reads run as ``git``
subprocesses too, never in-process through libgit2: the interpreter's GIL stays
free while a read waits, and a read that hangs is killed at its timeout (#436).

Implementation is split across mixin modules for maintainability:
- git_cache.py         — TTL-based result cache
- git_branch_ops.py    — Branch operations (list, create, delete, checkout, etc.)
- git_worktree_ops.py  — Worktree management (list, add, remove, prune)
- git_diff_ops.py      — Diff statistics and file diffs
- git_commit_ops.py    — Commit, push, pull, stage, stash operations
"""

import logging
import os
import re
import signal
import stat
import subprocess
import tempfile
import threading
import time
from concurrent.futures import Future
from typing import Any, Dict, List, Optional, Tuple

from fluid.tools.child_env import child_env_with_overlay, scrub_child_env

# Mixins composing GitService
from app.core.git_branch_ops import GitBranchMixin
from app.core.git_cache import (
    AHEAD_BEHIND_CACHE_TTL as _AHEAD_BEHIND_CACHE_TTL,
)
from app.core.git_cache import (
    STATUS_CACHE_TTL as _STATUS_CACHE_TTL,
)

from app.core.git_cache import (
    STATUS_STALE_TTL as _STATUS_STALE_TTL,
)

# Cache primitives used below
from app.core.git_cache import (
    _git_cache,
    checkout_key,
)
from app.core.fs_utils import force_rmtree
from app.core.git_commit_ops import GitCommitMixin
from app.core.git_diff_ops import GitDiffMixin
from app.core.git_worktree_ops import GitWorktreeMixin
from app.core.scrub import redact_url_credentials, strip_url_userinfo

logger = logging.getLogger(__name__)


# Clone sources, read the way git reads them (connect.c / transport.c).
_REMOTE_CLONE_SCHEMES = frozenset({"http", "https", "ssh", "git+ssh", "ssh+git"})
_URL_SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*)://")
# ``<transport>::<address>`` runs the remote helper git-remote-<transport>.
_REMOTE_HELPER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*::")
# scp-like ssh: ``[user@]host:path``. Neither user nor host may start with a
# dash (ssh would read it as an option).
_SCP_LIKE_RE = re.compile(
    r"^(?:[A-Za-z0-9._~+][A-Za-z0-9._~+\-]*@)?"
    r"(?:\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9][A-Za-z0-9.\-]*):\S+$"
)


# ``C:`` drive prefix (git's has_dos_drive_prefix).
_DOS_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def classify_clone_url(clone_url: str) -> Tuple[str, Optional[str]]:
    """How git will read *clone_url* as a clone source.

    Returns ``("remote", None)`` for an http(s) or ssh remote (``ssh://`` or
    scp-like ``user@host:path``), ``("local", path)`` for a filesystem path or
    a ``file://`` URL (*path* is the path git will open, possibly relative to
    the clone's cwd), and ``("invalid", reason)`` for everything else: option
    look-alikes, remote helpers (``ext::``, ``fd::``...), other URL schemes.
    """
    url = clone_url
    if not url.strip() or url.lstrip().startswith("-"):
        return "invalid", "Invalid clone URL (looks like a command-line option)"
    if _REMOTE_HELPER_RE.match(url):
        return "invalid", "Refusing unsafe clone URL scheme (remote helpers such as ext:: / file::)"
    match = _URL_SCHEME_RE.match(url)
    if match:
        scheme = match.group(1).lower()
        if scheme in _REMOTE_CLONE_SCHEMES:
            return "remote", None
        if scheme == "file":
            path = url[match.end():]
            # git percent-decodes file:// URLs; refuse encodings rather than
            # guess, and refuse a host part (``file://host/path``).
            if not path.startswith("/") or "%" in path:
                return "invalid", "A file:// clone URL must name an absolute local path"
            if os.name == "nt" and re.match(r"^/[A-Za-z]:/", path):
                # Canonical file:///C:/ URLs address C:/, not a root-relative
                # directory literally named C: under the current drive.
                path = path[1:]
            return "local", path
        return "invalid", "Unsupported clone URL: use an HTTPS or SSH URL"
    # Windows paths, read before the scp-like test the way git for Windows
    # does (connect.c url_is_local_not_ssh + has_dos_drive_prefix): a
    # one-letter "host" followed by ':' is a drive letter, never ssh. Applied
    # on every platform, since a POSIX server never has a one-letter ssh host
    # worth the ambiguity. ``C:\\repo`` / ``C:/repo`` are absolute local
    # paths; a drive-relative ``C:repo`` depends on that drive's cwd, so it is
    # refused rather than guessed. UNC and ``\\?\`` device paths are local.
    if _DOS_DRIVE_RE.match(url):
        if len(url) > 2 and url[2] in "\\/":
            return "local", url
        return "invalid", "A Windows clone path must be absolute (C:\\path\\to\\repo)"
    if url.startswith("\\\\"):
        return "local", url
    colon, slash = url.find(":"), url.find("/")
    if colon != -1 and (slash == -1 or colon < slash):
        # No ``/`` before the first ``:`` -> git reads it as scp-like ssh.
        if _SCP_LIKE_RE.match(url):
            return "remote", None
        return "invalid", "Unsupported clone URL: use an HTTPS or SSH URL"
    return "local", url


def _quiet_git_env(run_env: Dict[str, str]) -> None:
    """Keep the app's own git calls from disturbing the checkouts they read.

    * ``GIT_OPTIONAL_LOCKS=0``: a background ``git status`` never takes
      index.lock to refresh the index, so it cannot make an agent's own
      ``git add`` or ``git commit`` in the same worktree fail.
    * ``core.fsmonitor=false`` for this call only: with the setting in a
      repository's config (older releases wrote it), every git call in every
      worktree started a detached fsmonitor daemon on Windows and macOS that
      never exited (#436). The repository config itself is left alone.

    A caller's explicit values win; ``GIT_CONFIG_COUNT`` entries are appended to.
    """
    run_env.setdefault("GIT_OPTIONAL_LOCKS", "0")
    try:
        count = int(run_env.get("GIT_CONFIG_COUNT") or 0)
    except ValueError:
        return  # git rejects a malformed count anyway; do not reinterpret it
    run_env[f"GIT_CONFIG_KEY_{count}"] = "core.fsmonitor"
    run_env[f"GIT_CONFIG_VALUE_{count}"] = "false"
    run_env["GIT_CONFIG_COUNT"] = str(count + 1)


# =============================================================================
# Status reads: one in flight per checkout, cached, never raising
# =============================================================================

# One `git status` can take seconds on a cold, busy disk; past this it is
# killed and the last good status (or "unknown") is served instead.
STATUS_TIMEOUT_SECONDS = 20
# A status includes discovery and short metadata commands as well as status.
# Bound shared-future waits too, even if a reader fails outside subprocess code.
STATUS_WAIT_TIMEOUT_SECONDS = 2 * STATUS_TIMEOUT_SECONDS + 60
_GIT_KILL_TIMEOUT_SECONDS = 2
_GIT_REAP_TIMEOUT_SECONDS = 1
_IS_WINDOWS = os.name == "nt"


def _kill_git_tree(proc: subprocess.Popen) -> None:
    """End the tree before reaping the redirector, with bounded cleanup."""
    try:
        if _IS_WINDOWS:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
                timeout=_GIT_KILL_TIMEOUT_SECONDS, env=scrub_child_env(),
            )
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        logger.debug("Git process-tree cleanup failed", exc_info=True)
    finally:
        try:
            proc.kill()
        except OSError:
            pass  # The tree kill may already have reaped the redirector.
        try:
            proc.wait(timeout=_GIT_REAP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            pass  # Never turn a command timeout into an unbounded reap.


class StatusUnavailable(Exception):
    """A checkout's status could not be read (corrupt metadata, timeout...)."""


def _empty_status() -> Dict[str, Any]:
    return {
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
        "matches_base_tree": None,
        "conflicts": [],
    }


class _StatusReads:
    """Single-flight bookkeeping for status reads, keyed by checkout.

    * At most one read per checkout runs at a time; callers that arrive while
      it runs wait for it and share its result.
    * ``invalidate`` bumps the checkout's generation. Later callers wait for
      the old read to finish, then coalesce on one follow-up for the latest
      generation. Invalidations are not a queue of concurrent reads.
    * The last failure per checkout is kept so callers can tell a real clean
      status from the empty fallback (see GitService.get_status_error).
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.generation: Dict[str, int] = {}
        self.inflight: Dict[str, Tuple[int, Future]] = {}
        self.errors: Dict[str, str] = {}

    def invalidate(self, key: str) -> None:
        with self.lock:
            self.generation[key] = self.generation.get(key, 0) + 1

    def current(self, key: str) -> int:
        with self.lock:
            return self.generation.get(key, 0)


_status_reads = _StatusReads()


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
            env: Optional explicit subprocess environment. CLI-only cloud
                authentication is scrubbed from both inherited and explicit maps.
                ``None`` inherits the scrubbed parent environment.

        Returns:
            CompletedProcess with returncode, stdout, stderr
        """
        # Never let git (or a credential helper it spawns) wait on a terminal
        # prompt: the server has no TTY, so a fetch/clone that needs
        # credentials would otherwise sit until the timeout. The caller's env
        # wins when it sets the variable explicitly.
        run_env = child_env_with_overlay(env, {}) if env is not None else scrub_child_env()
        run_env.setdefault("GIT_TERMINAL_PROMPT", "0")
        _quiet_git_env(run_env)
        try:
            command = ["git"] + args
            group = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
                     if _IS_WINDOWS else {"start_new_session": True})
            # Files, not PIPEs: Windows communicate() can wait forever draining
            # pipes inherited by Git for Windows' redirected child. A finite
            # file snapshot needs no drain, even if tree cleanup fails.
            with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
                proc = subprocess.Popen(
                    command, cwd=working_dir, stdout=stdout, stderr=stderr,
                    env=run_env, **group,
                )
                try:
                    proc.wait(timeout=timeout)
                except BaseException:
                    _kill_git_tree(proc)
                    raise
                stdout.seek(0)
                stderr.seek(0)

                def captured(stream):
                    text = stream.read(os.fstat(stream.fileno()).st_size).decode("utf-8", "replace")
                    return text.replace("\r\n", "\n").replace("\r", "\n")

                result = subprocess.CompletedProcess(
                    command, proc.returncode, captured(stdout), captured(stderr),
                )
            if check:
                result.check_returncode()
            return result
        except subprocess.TimeoutExpired:
            logger.warning(
                "Git command timed out: git %s", redact_url_credentials(" ".join(args))
            )
            raise
        except subprocess.CalledProcessError as e:
            logger.warning(
                "Git command failed: git %s: %s",
                redact_url_credentials(" ".join(args)),
                redact_url_credentials(e.stderr or ""),
            )
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
                self.invalidate_status_cache(working_dir)
                return True
            logger.warning(f"Failed to init repo: {result.stderr}")
            return False
        except Exception as e:
            logger.error(f"Error initializing git repository: {e}")
            return False

    def rollback_init(self, working_dir: str, metadata_identity: Tuple[int, int]) -> None:
        """Remove only the metadata directory claimed by this initialization."""
        git_dir = os.path.join(working_dir, ".git")
        try:
            current = os.lstat(git_dir)
        except FileNotFoundError:
            return
        if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != metadata_identity:
            logger.warning("Not rolling back replaced Git metadata at %s", git_dir)
            return
        force_rmtree(git_dir)
        self.invalidate_status_cache(working_dir)

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
        # as an option. Reject remote-helper schemes (ext::, file::, fd::...)
        # which can run arbitrary local commands. Which sources a caller may
        # clone at all (remote vs. local disk) is the route's decision; see
        # classify_clone_url. Fail closed with the tuple contract rather than
        # raising, so callers surface a clean error instead of a 500.
        stripped = clone_url.strip()
        safe_url = redact_url_credentials(clone_url)
        if stripped.startswith("-"):
            logger.warning("Refusing clone URL that looks like an option: %s", safe_url[:50])
            return False, "Invalid clone URL (looks like a command-line option)"
        # Any ``<transport>::`` remote helper: ext:: runs a command, fd:: blocks
        # on a file descriptor, file:: reads the local disk.
        if _REMOTE_HELPER_RE.match(stripped):
            logger.warning("Refusing unsafe clone URL scheme: %s", safe_url[:50])
            return False, "Refusing unsafe clone URL scheme (ext:: / file::)"

        # Force-disable git's interactive credential prompt so a clone of a
        # private/credential-requiring remote fails fast instead of blocking on a
        # username/password prompt until the timeout. Merged onto (never replacing)
        # the inherited environment so existing git config/PATH survive.
        clone_env = {**scrub_child_env(), "GIT_TERMINAL_PROMPT": "0"}

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
                logger.debug("Cloned repository from %s into %s", safe_url, working_dir)
                self.invalidate_status_cache(working_dir)
                return True, None
            # git's stderr can echo the URL (and so its credentials) back.
            error = redact_url_credentials(result.stderr.strip()) or "Unknown error"
            logger.warning("Failed to clone repo: %s", error)
            return False, error
        except subprocess.TimeoutExpired:
            return False, f"Clone timed out after {timeout}s — the repository may be too large"
        except Exception as e:
            error = redact_url_credentials(str(e))
            logger.error("Error cloning repository: %s", error)
            return False, error

    def get_remote_url(self, working_dir: str) -> Optional[str]:
        """Get the remote URL for origin."""
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

        Call after anything that can change a checkout: the app's own
        mutations (commit, checkout, stage, push, pull...), the end of an agent
        turn in it, or an explicit refresh. The next read is fresh, queued
        behind any read already in flight (whose result is not reused/cached).
        """
        key = checkout_key(working_dir)
        # The cached status stays (tagged with its generation, so it is no
        # longer served as fresh) as the fallback for a read that times out.
        _status_reads.invalidate(key)
        _git_cache.invalidate(f"repo_root:{key}")
        _git_cache.invalidate(f"remote_url:{key}")
        _git_cache.invalidate_prefix(f"diff_stats:{key}:")
        _git_cache.invalidate_prefix(f"per_file_diff:{key}")
        _git_cache.invalidate_prefix(f"ahead_behind:{key}:")

    @staticmethod
    def status_generation(working_dir: str) -> int:
        """Bumped by every invalidate_status_cache of *working_dir*.

        Lets a holder of an older status (the worktree list) see that the
        checkout changed since it read it.
        """
        return _status_reads.current(checkout_key(working_dir))

    @staticmethod
    def get_status_error(working_dir: str) -> Optional[str]:
        """Why the last status read of *working_dir* failed, if it did.

        get_status never raises; on a failure it returns the empty "not a
        repository" dict, and this tells that fallback from a real answer.
        Cleared by the next successful read.
        """
        return _status_reads.errors.get(checkout_key(working_dir))

    def get_status(self, working_dir: str) -> Dict[str, Any]:
        """
        Get repository status. Never raises.

        Results are cached for a short TTL to coalesce rapid polls, at most one
        read per checkout runs at a time (concurrent callers share it), and a
        read that fails or times out serves the last good status if there is
        a recent one, otherwise the empty dict with get_status_error set.

        Untracked files are listed as git's default (``normal``) mode does: a
        directory holding only untracked files is one ``dir/`` entry, so a
        new tree is not walked file by file on every poll. Callers that need
        each file use expand_untracked_dirs.

        Args:
            working_dir: Repository directory

        Returns:
            Dictionary with:
            - is_git_repo: Whether this is a git repository
            - current_branch: Current branch name
            - is_detached: Whether HEAD is detached
            - head_commit: Current HEAD commit SHA
            - remote_url: Origin remote URL, userinfo (a clone token) stripped
            - is_clean: True if no uncommitted changes
            - staged: List of staged files
            - modified: List of modified files
            - untracked: List of untracked files
            - ahead: Commits ahead of upstream
            - behind: Commits behind upstream
            - conflicts: List of files with merge conflicts
        """
        key = checkout_key(working_dir)
        cache_key = f"status:{key}"
        reads = _status_reads
        deadline = time.monotonic() + STATUS_WAIT_TIMEOUT_SECONDS
        while True:
            # Check/cache/claim under the generation lock: invalidation cannot
            # race a cache publication, and an older flight is NEVER replaced.
            previous = None
            with reads.lock:
                generation = reads.generation.get(key, 0)
                cached = _git_cache.peek(cache_key)
                if cached is not None:
                    (cached_generation, cached_status), age = cached
                    if cached_generation == generation and age < _STATUS_CACHE_TTL:
                        return cached_status
                    previous = (cached_status, age)
                flight = reads.inflight.get(key)
                if flight is None:
                    future = Future()
                    reads.inflight[key] = (generation, future)
                    break
                future = flight[1]
            try:
                result = future.result(timeout=max(0, deadline - time.monotonic()))
            except TimeoutError:
                if previous is not None:
                    cached = _git_cache.peek(cache_key)
                    if cached is not None and cached[1] < _STATUS_STALE_TTL:
                        return cached[0][1]
                reads.errors[key] = "git status timed out waiting for an active read"
                return _empty_status()
            if flight[0] == generation:
                return result
            # The active read predates our request. All pending callers retry
            # the same slot for the latest generation, not one slot per change.
            if time.monotonic() >= deadline:
                reads.errors[key] = "git status timed out waiting for a fresh read"
                return _empty_status()

        try:
            result, fresh = self._read_status(working_dir, key, previous)
            with reads.lock:
                current = reads.generation.get(key, 0) == generation
                if fresh and current and result.get("is_git_repo"):
                    _git_cache.put(cache_key, (generation, result))
                del reads.inflight[key]
            future.set_result(result)
            return result
        except BaseException as exc:  # _read_status does not raise; belt and braces
            future.set_exception(exc)
            raise
        finally:
            with reads.lock:
                if reads.inflight.get(key, (None, None))[1] is future:
                    del reads.inflight[key]

    def _read_status(self, working_dir: str, key: str, previous) -> Tuple[Dict[str, Any], bool]:
        """One read, as ``(status, fresh)``.

        On a timeout the recent *previous* ``(status, age)`` is served (not
        fresh); on any other failure, or with nothing recent, the empty status.
        """
        errors = _status_reads.errors
        try:
            status = self._get_status_cli(working_dir)
        except (StatusUnavailable, subprocess.SubprocessError, OSError) as exc:
            message = str(exc) or type(exc).__name__
            if isinstance(exc, subprocess.TimeoutExpired):
                message = f"git status timed out after {exc.timeout:g}s"
                if previous is not None and previous[1] < _STATUS_STALE_TTL:
                    logger.warning("%s in %s; serving the status from %.0fs ago",
                                   message, working_dir, previous[1])
                    return previous[0], False
            if errors.get(key) == message:
                logger.debug("git status failed for %s (repeat): %s", working_dir, message)
            else:
                logger.warning("git status failed for %s: %s", working_dir, message)
            errors[key] = message
            return _empty_status(), False
        if errors.pop(key, None) is not None:
            logger.info("git status recovered for %s", working_dir)
        return status, True

    def _is_repo_root(self, working_dir: str, key: str) -> bool:
        """Whether *working_dir* is its repository's root (cached briefly).

        A folder with no ``.git`` entry of its own never is, so ordinary
        folders cost no subprocess. Otherwise git's own discovery decides: a
        broken ``.git`` inside another repository must not report that
        repository's changes. A probe that times out raises (and is not
        cached) rather than reporting "not a repository".
        """
        if not os.path.lexists(os.path.join(working_dir, ".git")):
            return False
        cache_key = f"repo_root:{key}"
        cached = _git_cache.get(cache_key, _AHEAD_BEHIND_CACHE_TTL)
        if cached is not None:
            return cached
        result = self._run_git(
            working_dir, ["rev-parse", "--show-toplevel"], timeout=STATUS_TIMEOUT_SECONDS
        )
        is_root = result.returncode == 0 and (
            os.path.realpath(result.stdout.strip()) == os.path.realpath(working_dir)
        )
        _git_cache.put(cache_key, is_root)
        return is_root

    def _get_status_cli(self, working_dir: str) -> Dict[str, Any]:
        """Read status with one ``git status`` (plus cached base-branch facts).

        Raises StatusUnavailable, or the subprocess error, when git cannot
        read the checkout.
        """
        key = checkout_key(working_dir)
        status = _empty_status()

        # Only the repository root counts: a project folder nested inside
        # another repo must not report — or let the UI act on — the enclosing
        # repo's changes.
        if not self._is_repo_root(working_dir, key):
            return status

        # ``-z`` keeps paths verbatim: without it git C-quotes non-ASCII names.
        # ``--branch`` adds HEAD's name and commit, so one call covers both.
        result = self._run_git(
            working_dir,
            ["status", "--porcelain=v2", "--branch", "-z", "--untracked-files=normal"],
            timeout=STATUS_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            detail = (result.stderr or "").strip().splitlines()
            raise StatusUnavailable(detail[0] if detail else f"git status exited with {result.returncode}")

        status["is_git_repo"] = True
        head_oid, head_name = _parse_status_v2(result.stdout, status)
        if head_oid and head_oid != "(initial)":
            status["head_commit"] = head_oid[:12]
            if head_name == "(detached)":
                status["is_detached"] = True
            else:
                status["current_branch"] = head_name

        status["remote_url"] = self._cached_remote_url(working_dir, key)
        if status["current_branch"]:
            status.update(self._base_comparison(
                working_dir, key, status["current_branch"], head_oid
            ))

        status["is_clean"] = not (
            status["staged"] or status["modified"] or status["untracked"] or status["conflicts"]
        )
        return status

    def _cached_remote_url(self, working_dir: str, key: str) -> Optional[str]:
        cache_key = f"remote_url:{key}"
        cached = _git_cache.get(cache_key, _AHEAD_BEHIND_CACHE_TTL)
        if cached is not None:
            return cached or None
        try:
            result = self._run_git(working_dir, ["remote", "get-url", "origin"], timeout=5)
        except (subprocess.SubprocessError, OSError) as exc:
            logger.debug("Reading the origin URL failed in %s: %s", working_dir, exc)
            return None
        url = strip_url_userinfo(result.stdout.strip()) if result.returncode == 0 else None
        _git_cache.put(cache_key, url or "")
        return url

    def _base_comparison(
        self, working_dir: str, key: str, branch: str, head_oid: str
    ) -> Dict[str, Any]:
        """Ahead/behind the default branch, and unpushed vs origin/<branch>.

        Ahead/behind relative to the default branch (main/master) is more
        useful for the PR workflow than the upstream tracking branch (which
        would be 0 after pushing). Cached by branch AND HEAD commit: rev-list
        walks the commit graph, and the answer only changes when HEAD moves
        (a new key) or the base refs do (fetch/push invalidate; otherwise
        the TTL bounds it).
        """
        cache_key = f"ahead_behind:{key}:{branch}:{head_oid}"
        cached = _git_cache.get(cache_key, _AHEAD_BEHIND_CACHE_TTL)
        if cached is not None:
            return dict(cached)

        info: Dict[str, Any] = {
            "ahead": 0, "behind": 0, "differs_from_base": False,
            "matches_base_tree": None, "unpushed": None,
        }
        try:
            self._compare_with_base(working_dir, branch, info)
        except (subprocess.SubprocessError, OSError) as exc:
            # Keep the file status; the counts are retried on the next read.
            logger.debug("Comparing %s with its base failed in %s: %s", branch, working_dir, exc)
            return info
        _git_cache.put(cache_key, tuple(info.items()))
        return info

    def _compare_with_base(self, working_dir: str, branch: str, info: Dict[str, Any]) -> None:
        default_branch = self.get_default_branch(working_dir)
        for ref in [f"origin/{default_branch}", default_branch]:
            result = self._run_git(
                working_dir,
                ["rev-list", "--left-right", "--count", f"{branch}...{ref}"],
                timeout=5,
            )
            if result.returncode != 0:
                continue
            parts = result.stdout.strip().split()
            if len(parts) >= 2:
                info["ahead"] = int(parts[0])
                info["behind"] = int(parts[1])
            # Only commits with net content over the base count as shippable:
            # a three-dot (base...HEAD) diff is empty when commits cancel out
            # or the branch is already merged. `git diff --quiet` exits 1 on
            # differences, 0 if none; any other code (e.g. missing ref) →
            # assume content so we don't hide a control that should show.
            if info["ahead"] > 0:
                diff_check = self._run_git(
                    working_dir, ["diff", "--quiet", f"{ref}...HEAD"], timeout=5
                )
                info["differs_from_base"] = diff_check.returncode != 0
            tree_check = self._run_git(
                working_dir, ["diff", "--quiet", ref, "HEAD", "--"], timeout=5
            )
            if tree_check.returncode in (0, 1):
                info["matches_base_tree"] = tree_check.returncode == 0
            break

        # Unpushed vs origin/<branch>. None when that remote ref does not exist
        # (branch never pushed) — NOT the same as unpushed=0 (fully pushed).
        # The sidebar stage ladder needs the distinction: a brand-new worktree
        # is ahead=0/unpushed=None, not "pushed, no PR yet".
        own_remote = f"origin/{branch}"
        check = self._run_git(
            working_dir, ["rev-parse", "--verify", "--quiet", own_remote], timeout=5
        )
        if check.returncode == 0:
            ab = self._run_git(
                working_dir,
                ["rev-list", "--left-right", "--count", f"HEAD...{own_remote}"],
                timeout=5,
            )
            if ab.returncode == 0:
                parts = ab.stdout.strip().split()
                try:
                    info["unpushed"] = int(parts[0]) if parts else None
                except ValueError:
                    info["unpushed"] = None

    def expand_untracked_dirs(self, working_dir: str, untracked: List[str]) -> List[str]:
        """Replace each untracked ``dir/`` entry with the files under it.

        For views that list every new file (the changed-files panel). One
        ``git ls-files`` for all directories; on failure the entries are kept.
        """
        dirs = [entry for entry in untracked if entry.endswith("/")]
        if not dirs:
            return list(untracked)
        try:
            result = self._run_git(
                working_dir,
                ["ls-files", "--others", "--exclude-standard", "-z", "--", *dirs],
                timeout=STATUS_TIMEOUT_SECONDS,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            logger.warning("Listing untracked files failed in %s: %s", working_dir, exc)
            return list(untracked)
        if result.returncode != 0:
            return list(untracked)
        files = [entry for entry in untracked if not entry.endswith("/")]
        files.extend(path for path in result.stdout.split("\0") if path)
        return files


def _parse_status_v2(output: str, status: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """Fold ``git status --porcelain=v2 --branch -z`` into *status*.

    Returns ``(branch.oid, branch.head)``. Index codes use the porcelain v1
    letters; v2 writes ``.`` for "unchanged".
    """
    head_oid: Optional[str] = None
    head_name: Optional[str] = None
    entries = output.split("\0")
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if not entry:
            continue
        if entry.startswith("# branch.oid "):
            head_oid = entry[len("# branch.oid "):]
            continue
        if entry.startswith("# branch.head "):
            head_name = entry[len("# branch.head "):]
            continue
        kind = entry[0]
        source = None
        if kind == "?":
            status["untracked"].append(entry[2:])
            continue
        if kind == "u":
            # Unmerged (both modified, added by both, deleted by them...).
            status["conflicts"].append(entry.split(" ", 10)[10])
            continue
        if kind == "1":
            fields = entry.split(" ", 8)
            path = fields[8]
        elif kind == "2":
            # Rename or copy: -z emits the source as the next entry, for a
            # worktree rename (intent-to-add) as well.
            fields = entry.split(" ", 9)
            path = fields[9]
            source = entries[index] if index < len(entries) else None
            index += 1
        else:
            continue  # "!" (ignored) and headers this parser does not use
        index_code, worktree_code = fields[1][0], fields[1][1]
        if index_code in "AMDRCT":
            status["staged"].append({"file": path, "status": index_code})
        # An unstaged deletion, type change or rename is a modification too; a
        # renamed file's source is gone from disk. A worktree "A" is an
        # intent-to-add entry (``git add -N``): its content is not staged yet.
        if worktree_code in "MDTRA":
            status["modified"].append(path)
            if worktree_code == "R" and source:
                status["modified"].append(source)
    return head_oid, head_name


# Global instance
git_service = GitService()
