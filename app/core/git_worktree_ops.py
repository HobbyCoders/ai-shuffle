"""
Git Worktree Operations Mixin

Provides worktree management: list, add, remove, and prune worktrees.
"""

import logging
import os
import threading
from typing import Any, Dict, List, Optional, Tuple

from app.core.git_cache import _native_reader

logger = logging.getLogger(__name__)

# git serialises index and ref updates, but not worktree creation: two
# concurrent `git worktree add` calls on one repository can read each other's
# half-written `.git/worktrees/<name>/commondir` and die with "failed to read
# ... commondir". Every worktree mutation of a repository takes its lock.
_REPO_LOCKS: Dict[str, threading.RLock] = {}
_REPO_LOCKS_GUARD = threading.Lock()


def repo_worktree_lock(main_dir: str) -> threading.RLock:
    """The process-wide lock serialising worktree add/remove/prune for a repository."""
    key = os.path.realpath(main_dir)
    with _REPO_LOCKS_GUARD:
        lock = _REPO_LOCKS.get(key)
        if lock is None:
            lock = _REPO_LOCKS[key] = threading.RLock()
        return lock


def forget_stale_worktree_metadata(main_dir: str, worktree_path: str) -> None:
    """Forget ONE stale Git registration, touching only its administrative files.

    Git's prune is repository-wide; worktree remove may recurse through Windows
    junctions. Neither is suitable here. Verify the common-dir/back-reference,
    preflight every admin entry without following links, then unlink only those
    files. The checkout, dependency links and branch refs are never touched.
    """
    import stat
    import subprocess
    from pathlib import Path

    from app.core.worktree_report import _git, _is_checkout_root, parse_worktree_list

    def key(path):
        return os.path.normcase(os.path.abspath(path))

    metadata_root = None
    metadata_device = None

    def no_links(path):
        mounts = set()
        if metadata_root is not None and os.sys.platform == "linux":
            # st_dev catches filesystem crossings but not same-device bind
            # mounts. Refresh Linux's mount table on every delete preflight.
            for line in Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines():
                field = line.split()[4]
                for escaped, plain in ((r"\040", " "), (r"\011", "\t"), (r"\012", "\n"), (r"\134", "\\")):
                    field = field.replace(escaped, plain)
                mounts.add(Path(field))
        for part in (path, *path.parents):
            try:
                info = part.lstat()
            except FileNotFoundError:
                continue  # Only genuine absence is safe; inspection errors propagate.
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("path contains a symlink or Windows junction")
            if metadata_device is not None and (part == metadata_root or metadata_root in part.parents):
                if info.st_dev != metadata_device or part in mounts:
                    raise ValueError("Git administrative path crosses a foreign filesystem or mount")

    target = Path(os.path.abspath(worktree_path))
    main = Path(os.path.abspath(main_dir))
    # Only the app's own placement roots; never forget a base or imported clone.
    roots = [main / name / "worktrees" for name in (".fluid", ".claude")]
    if not any(key(root) != key(target) and root in target.parents for root in roots):
        raise ValueError("not an app-managed worktree path")
    no_links(target)
    if not _is_checkout_root(str(main)):
        raise ValueError("project checkout is unavailable or invalid")

    def require_stale():
        # A real but switched/foreign/borrowed Git root is not a stale directory.
        if _is_checkout_root(str(target)) or (
            (target / ".git").exists()
            and _git(str(target), "rev-parse", "--absolute-git-dir") is not None
        ):
            raise ValueError("directory still has usable Git metadata; keep its registration")

    require_stale()
    common = _git(str(main), "rev-parse", "--path-format=absolute", "--git-common-dir")
    listing = _git(str(main), "worktree", "list", "--porcelain")
    if not common or listing is None:
        raise ValueError("Git registration could not be verified")
    item = next((i for i in parse_worktree_list(listing) if key(i["path"]) == key(target)), None)
    if item and item.get("locked"):
        raise ValueError("worktree is locked")
    metadata_root = Path(common.strip())
    no_links(metadata_root)
    metadata_device = metadata_root.lstat().st_dev
    admin_root = metadata_root / "worktrees"
    no_links(admin_root)
    matches = []
    if admin_root.is_dir():
        for admin in admin_root.iterdir():
            no_links(admin)
            backlink = admin / "gitdir"
            if not admin.is_dir() or not backlink.is_file():
                continue
            no_links(backlink)
            link = Path(backlink.read_text(encoding="utf-8").strip())
            if not link.is_absolute():
                link = admin / link
            if key(link) == key(target / ".git"):
                matches.append(admin)
    if not matches and not item:
        return  # Registry-only: no Git metadata to prune.
    if len(matches) != 1:
        raise ValueError("Git administrative back-reference is missing or ambiguous")
    admin = matches[0]
    if (admin / "locked").exists():
        raise ValueError("worktree is locked")
    files, directories, fsmonitor_sockets = [], [], []
    pending = [admin]
    while pending:
        directory = pending.pop()
        no_links(directory)
        directories.append(directory)
        for entry in directory.iterdir():
            no_links(entry)
            info = entry.lstat()
            if entry.name.endswith(".lock"):
                raise ValueError("Git administrative files are busy")
            if stat.S_ISDIR(info.st_mode):
                pending.append(entry)
            elif stat.S_ISREG(info.st_mode):
                files.append(entry)
            elif entry.parent == admin and entry.name == "fsmonitor--daemon.ipc" and stat.S_ISSOCK(info.st_mode):
                fsmonitor_sockets.append(entry)
            else:
                raise ValueError(f"unexpected Git administrative file type: {entry.name} ({stat.filemode(info.st_mode)})")
    # Fail closed if the checkout was restored while gathering the manifest.
    require_stale()
    for endpoint in fsmonitor_sockets:
        # Newer Linux Git leaves a Unix IPC socket in each worktree admin dir.
        # Stop only that daemon, after validating the whole manifest, so it
        # cannot keep writing into metadata we are about to forget. Explicit
        # --git-dir scopes IPC to this admin dir, never the main daemon's socket.
        no_links(endpoint)
        try:
            stopped = subprocess.run(
                ["git", "-C", str(main), f"--git-dir={admin}", f"--work-tree={main}", "fsmonitor--daemon", "stop"],
                capture_output=True, text=True, timeout=30,
                env={**os.environ, "LC_ALL": "C"},
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError("Git fsmonitor daemon could not be stopped") from exc
        # A vanished checkout can leave its socket after the daemon has exited.
        # Only Git's explicit no-daemon result is safe to treat as stopped.
        if stopped.returncode and not (
            stopped.returncode == 128 and stopped.stderr.strip() == "fatal: fsmonitor--daemon is not running"
        ):
            raise ValueError("Git fsmonitor daemon could not be stopped")
        if endpoint.exists():
            no_links(endpoint)
            if not stat.S_ISSOCK(endpoint.lstat().st_mode):
                raise ValueError("Git fsmonitor endpoint changed type")
            endpoint.unlink()
    require_stale()
    for entry in files:
        no_links(entry)
        entry.unlink()
    for directory in reversed(directories):
        no_links(directory)
        directory.rmdir()


class GitWorktreeMixin:
    """Mixin providing worktree operations for GitService."""

    def add_worktree(self, main_dir: str, path: str, branch: str, new_branch: bool = False,
                     base_branch: Optional[str] = None) -> Tuple[bool, Optional[str]]:
        """Add a worktree; serialised per repository (see repo_worktree_lock)."""
        with repo_worktree_lock(main_dir):
            return self._add_worktree_unlocked(main_dir, path, branch, new_branch=new_branch, base_branch=base_branch)

    def remove_worktree(self, main_dir: str, path: str, force: bool = False) -> bool:
        """Remove a worktree; serialised per repository (see repo_worktree_lock)."""
        with repo_worktree_lock(main_dir):
            return self._remove_worktree_unlocked(main_dir, path, force=force)

    def prune_worktrees(self, main_dir: str) -> bool:
        """Prune stale worktree entries; serialised per repository (see repo_worktree_lock)."""
        with repo_worktree_lock(main_dir):
            return self._prune_worktrees_unlocked(main_dir)


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

    def _add_worktree_unlocked(
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

    def _remove_worktree_unlocked(
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

    def _prune_worktrees_unlocked(self, main_dir: str) -> bool:
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
