"""
Git Worktree Operations Mixin

Provides worktree management: list, add, remove, and prune worktrees.
"""

import asyncio
import errno
import hashlib
import logging
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# git serialises index and ref updates, but not worktree creation: two
# concurrent `git worktree add` calls on one repository can read each other's
# half-written `.git/worktrees/<name>/commondir` and die with "failed to read
# ... commondir". Every worktree mutation of a repository takes its lock.
def _common_git_dir(path: str) -> Path:
    """Resolve main/linked/bare paths to the same lock identity without Git."""
    root = Path(os.path.realpath(path))
    git_dir = root / ".git"
    if git_dir.is_file():
        pointer = git_dir.read_text(encoding="utf-8").strip()
        if pointer.startswith("gitdir: "):
            git_dir = root / pointer.removeprefix("gitdir: ")
    elif not git_dir.is_dir():
        git_dir = root
    common = git_dir / "commondir"
    if common.is_file():
        git_dir = git_dir / common.read_text(encoding="utf-8").strip()
    return Path(os.path.normcase(os.path.realpath(git_dir)))


def _lock_contention(exc: OSError) -> bool:
    return exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK) or getattr(exc, "winerror", None) == 33


class WorktreeError(Exception):
    """User-facing worktree management refusal (re-exported by worktree_manager)."""


class WorktreeAddError(RuntimeError):
    """Git attempted a checkout and failed, rather than a missing base/branch."""


class WorktreeLockError(WorktreeAddError, WorktreeError, ValueError):
    """Bounded lock refusal for checkout, legacy-worktree and transfer callers."""


WORKTREE_LOCK_TIMEOUT = 60.0


def _wait_for_lock(attempt, *, blocking: bool, deadline: float) -> bool:
    from app.db.async_database import sync_cancelled

    while True:
        if sync_cancelled():
            raise asyncio.CancelledError("Checkout lock wait cancelled")
        if blocking and time.monotonic() >= deadline:
            raise WorktreeLockError("Timed out waiting for the checkout lock; retry when the other operation finishes.")
        try:
            if attempt():
                return True
        except OSError as exc:
            if not _lock_contention(exc):
                raise
        if not blocking:
            return False
        time.sleep(.1)


class _RepositoryLock:
    """Reentrant repository lock; all local/OS waits share one deadline.

    run_sync cancellation interrupts waiting, never a critical section already
    running. Direct synchronous callers are still bounded by the same deadline.
    """

    def __init__(self, common: Path):
        self.common = common
        self.thread_lock = threading.RLock()
        self.depth = 0
        self.file = None

    def acquire(self, blocking: bool = True) -> bool:
        # Nested validation is part of the already-running critical section,
        # not a new wait that cancellation may interrupt halfway through a write.
        if self.thread_lock.acquire(blocking=False):
            if self.depth:
                self.depth += 1
                return True
            self.thread_lock.release()
        deadline = time.monotonic() + WORKTREE_LOCK_TIMEOUT
        if not _wait_for_lock(lambda: self.thread_lock.acquire(blocking=False),
                              blocking=blocking, deadline=deadline):
            return False
        try:
            if self.depth == 0 and (self.common / "HEAD").is_file():
                handle = (self.common / "ai-shuffle-worktrees.lock").open("a+b")
                try:
                    def try_os_lock():
                        if os.name == "nt":
                            import msvcrt
                            handle.seek(0)
                            # LK_LOCK has an implicit ten-retry limit. Never use
                            # a blocking OS primitive: cancellation/deadline must
                            # be checked between attempts on every platform.
                            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        return True

                    if not _wait_for_lock(try_os_lock, blocking=blocking, deadline=deadline):
                        handle.close()
                        self.thread_lock.release()
                        return False
                except BaseException:
                    handle.close()
                    raise
                self.file = handle
            self.depth += 1
            return True
        except BaseException:
            self.thread_lock.release()
            raise

    def release(self) -> None:
        try:
            self.depth -= 1
            if self.depth == 0 and self.file is not None:
                # Closing releases the OS lock, also automatically on SIGKILL.
                self.file.close()
                self.file = None
        finally:
            self.thread_lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()


_REPO_LOCKS: Dict[str, _RepositoryLock] = {}
_REPO_LOCKS_GUARD = threading.Lock()


def repo_worktree_lock(main_dir: str) -> _RepositoryLock:
    """One common-Git-dir lock for mutation, validation and row publication.

    Never unlink its sidecar: waiters must keep locking the same inode.
    """
    common = _common_git_dir(main_dir)
    key = str(common)
    with _REPO_LOCKS_GUARD:
        lock = _REPO_LOCKS.get(key)
        if lock is None:
            lock = _REPO_LOCKS[key] = _RepositoryLock(common)
        return lock


def _is_lfs_failure(stderr: str) -> bool:
    """Require an LFS-specific diagnostic AND a failing filter/binary/hook."""
    # Do not classify a branch/path named "git-lfs" on Git's progress line
    # plus an unrelated failure (or a different driver's filter-process) as LFS.
    return any(
        not line.startswith("preparing worktree")
        and not re.search(r"invalid reference:|not a valid branch name|already (?:exists|used|checked out)", line)
        and re.search(r"git-lfs(?:\.exe)?(?=$|[\s:'\"])|filter[ .]lfs|filter ['\"]lfs['\"]", line)
        and re.search(
            r"not found|no such file|cannot (?:run|spawn|exec)|failed|failure|"
            r"hung up|not recognized|not installed|permission denied|exec format", line
        )
        for line in stderr.lower().splitlines()
    )


def worktree_error_detail(stderr: str) -> str:
    """Bound Git diagnostics before exposing them to a session or durable op."""
    from app.core.scrub import redact_url_credentials

    return redact_url_credentials((stderr or "Unknown git error").strip())[-2000:]


LFS_PASSTHROUGH_CONFIG = (
    "-c", "filter.lfs.required=false",
    "-c", "filter.lfs.process=",
    "-c", "filter.lfs.smudge=",
    "-c", "filter.lfs.clean=",
)

APP_WORKTREES_EXCLUDE = ".fluid/worktrees/"
WORKTREE_ADD_FAILED_MARKER = "ai-shuffle-worktree-add-failed"


def exclude_app_worktrees(main_dir: str, path: Optional[str] = None) -> None:
    """Keep the app's ``.fluid/worktrees/`` out of *main_dir*'s ``git status``.

    Every path that creates a checkout under ``<main_dir>/.fluid/worktrees/``
    calls this (#791); with *path* it does nothing unless *path* lies there.
    The entry goes in the repository's local ``info/exclude`` (in the common
    git dir when *main_dir* is itself a linked worktree), never the tracked
    ``.gitignore``. Idempotent and best effort: a failure only leaves the
    folder showing as untracked.
    """
    from pathlib import Path

    main = Path(os.path.abspath(main_dir))
    if path is not None and main / ".fluid" / "worktrees" not in Path(os.path.abspath(path)).parents:
        return
    try:
        git_dir = main / ".git"
        if git_dir.is_dir():
            info_dir = git_dir / "info"
        elif git_dir.is_file():
            from app.core.worktree_report import _git
            common = (_git(str(main), "rev-parse", "--git-common-dir", timeout=5) or "").strip()
            if not common:
                logger.warning("Could not resolve --git-common-dir for %s", main)
                return
            # rev-parse may answer relative to main_dir.
            info_dir = (main / common).resolve() / "info"
        else:
            return
        info_dir.mkdir(parents=True, exist_ok=True)
        exclude_file = info_dir / "exclude"
        existing = exclude_file.read_text(encoding="utf-8", errors="ignore") if exclude_file.exists() else ""
        if APP_WORKTREES_EXCLUDE in {line.strip() for line in existing.splitlines()}:
            return
        prefix = "" if (not existing or existing.endswith("\n")) else "\n"
        with exclude_file.open("a", encoding="utf-8") as f:
            f.write(f"{prefix}{APP_WORKTREES_EXCLUDE}\n")
        logger.debug("Added '%s' to %s", APP_WORKTREES_EXCLUDE, exclude_file)
    except Exception as e:  # noqa: BLE001 - never fail a checkout over an ignore rule
        logger.warning("Failed to update git info/exclude for %s: %s", main, e)


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

    from fluid.tools.child_env import scrub_child_env
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
                env={**scrub_child_env(), "LC_ALL": "C"},
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
                     base_branch: Optional[str] = None, *, fetch_base: bool = True) -> Tuple[bool, Optional[str]]:
        """Add a worktree; return (success, error or successful-fallback warning)."""
        with repo_worktree_lock(main_dir):
            exclude_app_worktrees(main_dir, path)
            had_git_marker = os.path.lexists(os.path.join(path, ".git"))
            if not had_git_marker:
                # Write ahead in common metadata: the per-worktree admin dir
                # does not exist yet, and a killed add cannot record its result.
                try:
                    marker = self._worktree_materializing_marker(main_dir, path)
                    marker.parent.mkdir(exist_ok=True)
                    marker.write_text("incomplete: materializing\n", encoding="utf-8")
                except Exception as exc:
                    return False, f"Cannot record checkout materialization: {worktree_error_detail(str(exc))}"
            result = self._add_worktree_unlocked(main_dir, path, branch, new_branch=new_branch,
                                                 base_branch=base_branch, fetch_base=fetch_base)
            if result[0]:
                try:
                    # Git's zero exit is not proof: hooks can remove files.
                    # All add callers, including legacy recreate and handoff,
                    # clear write-ahead state only after this full check.
                    self.require_complete_worktree(path, force=True)
                except Exception as exc:
                    return False, worktree_error_detail(str(exc))
            elif not had_git_marker:
                # Do not flag an existing user's checkout when Git simply
                # refuses to add over it. Keep write-ahead state on failure.
                self._record_worktree_add_result(main_dir, path, failed=True)
            return result

    def _worktree_materializing_marker(self, main_dir: str, path: str) -> Path:
        result = self._run_git(main_dir, ["rev-parse", "--git-common-dir"], timeout=10)
        if result.returncode or not (result.stdout or "").strip():
            raise WorktreeAddError(f"Cannot locate materialization metadata for {path!r}")
        key = hashlib.sha256(os.path.normcase(os.path.realpath(path)).encode()).hexdigest()
        return Path(main_dir) / result.stdout.strip() / "ai-shuffle-materializing" / key

    def require_complete_worktree(self, path: str, *, force: bool = False) -> None:
        """Validate fresh/unfinished materializations, never ordinary user edits.

        Path-scoped write-ahead state survives SIGKILL, even before Git creates
        its private worktree dir. Clear negative evidence only after validation.
        """
        with repo_worktree_lock(path):
            self._require_complete_worktree_locked(path, force=force)

    def _require_complete_worktree_locked(self, path: str, *, force: bool) -> None:
        pending = self._worktree_materializing_marker(path, path)
        result = self._run_git(path, ["rev-parse", "--git-path", WORKTREE_ADD_FAILED_MARKER], timeout=10)
        if result.returncode or not (result.stdout or "").strip():
            raise WorktreeAddError(f"Cannot locate checkout failure metadata for {path!r}")
        failed = Path(path) / result.stdout.strip()
        if not force and not pending.exists() and not failed.exists():
            return
        if not self._checkout_is_complete(path):
            # Preserve negative evidence even if an older/foreign reader
            # removed it. Row publication is separately guarded by this lock.
            pending.parent.mkdir(exist_ok=True)
            pending.write_text("incomplete: validation failed\n", encoding="utf-8")
            raise WorktreeAddError(
                f"Checkout {path!r} is incomplete (missing index or committed files); "
                "refusing to use or repair it because local changes may be present. "
                "Inspect and repair the checkout, then retry."
            )
        try:
            failed.unlink(missing_ok=True)
            pending.unlink(missing_ok=True)
        except OSError as exc:
            raise WorktreeAddError(f"Cannot clear checkout failure for {path!r}: {exc}") from exc

    def _record_worktree_add_result(self, main_dir: str, path: str, *, failed: bool) -> None:
        """Keep failure evidence in the registered worktree's private Git dir.

        Resolve through common metadata so even a registered remnant with no
        working directory/.git file can be flagged. Never change working files.
        """
        try:
            result = self._run_git(main_dir, ["rev-parse", "--git-common-dir"], timeout=10)
            if result.returncode or not (result.stdout or "").strip():
                return
            metadata = Path(main_dir) / result.stdout.strip() / "worktrees"
            if not metadata.is_dir():
                return
            target = os.path.normcase(os.path.abspath(os.path.join(path, ".git")))
            for admin in metadata.iterdir():
                if not admin.is_dir():
                    continue
                try:
                    registered = (admin / "gitdir").read_text(encoding="utf-8").strip()
                except OSError:
                    continue
                if os.path.normcase(os.path.abspath(registered)) != target:
                    continue
                marker = admin / WORKTREE_ADD_FAILED_MARKER
                if failed:
                    marker.write_text("incomplete: failed add\n", encoding="utf-8")
                else:
                    marker.unlink(missing_ok=True)
                return
        except Exception as exc:  # noqa: BLE001 - preserve Git's original outcome
            logger.warning("Could not record worktree add outcome at %s: %s", path, exc)

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
        # the primary's Windows path ``C:\Projects\example-project`` reaching a Linux
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
        base_branch: Optional[str] = None,
        *,
        fetch_base: bool = True,
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
            Tuple of (success, error or successful-fallback warning).
        """
        try:
            # Never delete an existing checkout or somebody else's files on retry.
            retry_safe = not os.path.lexists(path) or (
                os.path.isdir(path) and not os.path.islink(path) and not os.listdir(path)
            )
            # When creating a new branch from a base branch, fetch latest from origin first
            # This ensures the new branch includes all remote commits
            if new_branch and base_branch and fetch_base:
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
                return True, None

            if retry_safe and _is_lfs_failure(result.stderr or ""):
                original_error = worktree_error_detail(result.stderr)
                target = os.path.normcase(os.path.abspath(path))
                registered = any(os.path.normcase(os.path.abspath(wt["path"])) == target
                                 for wt in self.list_worktrees(main_dir))
                if registered:
                    # Registration can survive a failed checkout rollback too.
                    # Never remove/reset an adoptable checkout, and only accept
                    # a hook-only failure with independent completeness evidence.
                    from app.core.checkout_manager import _require_branch_binding

                    _require_branch_binding(path, branch)
                    if not self._checkout_is_complete(path):
                        logger.warning("LFS setup failed at %s; keeping incomplete checkout: %s", path, original_error)
                        return False, original_error
                    logger.warning("LFS hook failed at %s; keeping completed checkout: %s", path, original_error)
                else:
                    logger.warning("LFS checkout failed at %s; retrying without LFS: %s", path, original_error)
                    if not self._cleanup_failed_lfs_checkout(main_dir, path):
                        return False, original_error + " (could not safely clean up the failed checkout)"
                    if not self._prune_worktrees_unlocked(main_dir):
                        return False, original_error + " (could not prune the failed checkout)"

                    # -b can have created its ref even though checkout failed.
                    # Reuse that ref rather than trying to create it again.
                    if new_branch and self._run_git(
                        main_dir, ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], timeout=10
                    ).returncode == 0:
                        args = ["worktree", "add", "--", path, branch]
                    with tempfile.TemporaryDirectory(prefix="shuffle-no-lfs-hooks-") as hooks:
                        result = self._run_git(main_dir, [
                            *LFS_PASSTHROUGH_CONFIG,
                            "-c", f"core.hooksPath={hooks}", *args,
                        ], timeout=30)
                    if result.returncode != 0:
                        # Keep both causes, but never retry a second time.
                        return False, worktree_error_detail(
                            original_error + "\nRetry without LFS failed: " + (result.stderr or "Unknown git error")
                        )
                from fluid.context.runtime_facts import LFS_FALLBACK_WARNING, record_lfs_fallback

                record_lfs_fallback(path)
                logger.warning("%s Worktree: %s", LFS_FALLBACK_WARNING, path)
                return True, LFS_FALLBACK_WARNING

            error_msg = worktree_error_detail(result.stderr)
            logger.warning(f"Failed to add worktree: {error_msg}")
            return False, error_msg
        except Exception as e:
            error_msg = worktree_error_detail(str(e))
            logger.error("Error adding worktree: %s", error_msg)
            return False, error_msg

    def _checkout_is_complete(self, path: str) -> bool:
        """Check index/HEAD and missing files without invoking clean/smudge filters."""
        index = self._run_git(path, ["rev-parse", "--git-path", "index"], timeout=10)
        if index.returncode or not os.path.isfile(os.path.join(path, index.stdout.strip())):
            return False
        # Only staged deletions mean the checkout is missing committed content;
        # a hook may legitimately stage generated or modified files. --no-renames
        # keeps a staged rename visible as the deletion it is.
        cached = self._run_git(
            path, ["diff", "--cached", "--quiet", "--no-renames", "--diff-filter=D", "--no-ext-diff", "--no-textconv",
                   "HEAD", "--"], timeout=10
        )
        if cached.returncode:
            return False
        deleted = self._run_git(path, ["ls-files", "--deleted", "-z"], timeout=10)
        return deleted.returncode == 0 and not deleted.stdout

    def _cleanup_failed_lfs_checkout(self, main_dir: str, path: str) -> bool:
        """Clean only unregistered remnants, never an adoptable checkout."""
        if not os.path.lexists(path):
            return True
        target = os.path.normcase(os.path.abspath(path))
        if any(os.path.normcase(os.path.abspath(wt["path"])) == target
               for wt in self.list_worktrees(main_dir)):
            return False  # A registered checkout may already be in use.
        # Some Git/OS failures leave files after unregistering the checkout.
        # Still use the mount/junction preflight before removing that remnant.
        from app.core.checkout_manager import sever_runtime_links
        from app.core.fs_utils import force_rmtree

        if sever_runtime_links(path)["errors"]:
            return False
        force_rmtree(path)
        return True

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
            # Preflight the whole tree BEFORE git can recurse: detach even
            # unknown nested junctions/reparse points and refuse mount points.
            # Local import: checkout_manager itself imports git_service.
            from app.core.checkout_manager import sever_runtime_links

            sever_summary = sever_runtime_links(path)
            if sever_summary["errors"]:
                # Do not hand the directory to git while a junction may still
                # be attached — continuing is how the main checkout's
                # node_modules got deleted.
                logger.warning(
                    "Removal safety preflight in %s hit errors; refusing git "
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
