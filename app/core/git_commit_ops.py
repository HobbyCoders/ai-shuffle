"""
Git Commit Operations Mixin

Provides commit-related git operations: stage, unstage, discard,
commit, push, pull, stash, and conflict detection.
"""

import logging
import os
import unicodedata
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _is_link_like(path: str) -> bool:
    """True for a symlink or a Windows junction: removed as a link, never followed."""
    isjunction = getattr(os.path, "isjunction", None)
    return os.path.islink(path) or bool(isjunction and isjunction(path))


def _remove_link(path: str) -> None:
    """Remove the link *path* itself (a directory junction needs rmdir)."""
    try:
        os.unlink(path)
    except OSError:
        os.rmdir(path)


def _is_ntfs_dotgit(name: str) -> bool:
    """Whether NTFS reads path component *name* as ``.git``.

    Mirrors git's ``is_ntfs_dotgit``: case-insensitive, trailing dots and spaces
    ignored, an alternate data stream suffix (``.git::$INDEX_ALLOCATION``)
    ignored, and the 8.3 short name ``GIT~1``.
    """
    base = name.split(":", 1)[0].rstrip(" .").lower()
    return base in (".git", "git~1")


# Code points HFS+ drops when it compares names (git's ``next_hfs_char``):
# zero-width joiners and direction marks, the deprecated format characters and
# the byte-order mark. ``.g\u200cit`` names the ``.git`` directory there.
_HFS_IGNORED = frozenset(
    [0x200C, 0x200D, 0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x206A, 0x2070), 0xFEFF]
)


def _is_hfs_dotgit(name: str) -> bool:
    """Whether HFS+ reads path component *name* as ``.git``.

    Mirrors git's ``is_hfs_dotgit``: the ignorable code points above are
    dropped and the comparison folds case. The name is compared in both NFC
    and NFD, since HFS+ stores decomposed names and the caller may not.
    """
    for form in ("NFC", "NFD"):
        folded = "".join(
            ch for ch in unicodedata.normalize(form, name) if ord(ch) not in _HFS_IGNORED
        )
        if folded.casefold() == ".git":
            return True
    return False


def _is_dotgit_alias(name: str) -> bool:
    """Whether NTFS or HFS+ reads path component *name* as ``.git``."""
    return _is_ntfs_dotgit(name) or _is_hfs_dotgit(name)


def _untracked_discard_target(
    working_dir: str, real_working_dir: str, rel_path: str
) -> Optional[str]:
    """Return the path to delete when discarding untracked *rel_path*, or None.

    Only the final component is left unresolved, so an untracked symlink or
    junction is removed itself rather than the directory it points at. The
    target must sit strictly inside the checkout and outside its ``.git``
    metadata: discarding ``.`` or ``.git`` would otherwise delete the whole
    checkout or its history. The ``.git`` check applies to the spelling (every
    NTFS and HFS+ alias git itself refuses) and to the resolved identity, since
    ``realpath`` expands an 8.3 short name that ``relpath`` leaves as written
    and does not fold an HFS+ ignorable code point at all.
    """
    lexical = os.path.normpath(os.path.join(working_dir, rel_path))
    target = os.path.join(
        os.path.realpath(os.path.dirname(lexical)), os.path.basename(lexical)
    )
    if not target.startswith(real_working_dir + os.sep):
        return None
    first = os.path.relpath(target, real_working_dir).split(os.sep)[0]
    if _is_dotgit_alias(first):
        return None
    if not _is_link_like(target):
        real = os.path.realpath(target)
        if not real.startswith(real_working_dir + os.sep):
            return None
        if _is_dotgit_alias(os.path.relpath(real, real_working_dir).split(os.sep)[0]):
            return None
        try:
            if os.path.samefile(target, os.path.join(real_working_dir, ".git")):
                return None
        except OSError:
            pass
    return target


_REFNAME_FORBIDDEN = frozenset(" ~^:?*[\\")


def _is_plain_branch_name(branch: str) -> bool:
    """Whether *branch* is a plain branch name, never a refspec or an option.

    ``git push <remote> <branch>`` takes a refspec there, so ``:main`` deletes
    the remote branch, ``+x:main`` force-pushes past the lease and
    ``HEAD:refs/tags/v1`` overwrites any ref; a leading ``-`` parses as an
    option. The rules are ``git check-ref-format --branch``'s: no control
    characters, space or ``~^:?*[\\``, no ``..`` or ``@{``, no leading ``-``,
    no component that starts with ``.`` or ends with ``.lock``, and no leading,
    trailing or doubled ``/``. A leading ``+`` (a forced refspec) is refused too.
    """
    if not branch or branch == "@" or branch[0] in "-+" or branch.endswith((".", "/")):
        return False
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F or ch in _REFNAME_FORBIDDEN for ch in branch):
        return False
    if ".." in branch or "@{" in branch:
        return False
    return all(
        part and not part.startswith(".") and not part.endswith(".lock")
        for part in branch.split("/")
    )


def _push_refspec(branch: str) -> str:
    """The fully qualified refspec that pushes local *branch* to the same name.

    Qualified on both sides, so the remote side cannot resolve to a tag or
    another namespace, and nothing in it can parse as an option.
    """
    return f"refs/heads/{branch}:refs/heads/{branch}"


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
                    if _is_link_like(full_path):
                        _remove_link(full_path)
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
        *,
        allow_empty: bool = False,
        fallback_identity: Optional[Tuple[str, str]] = None,
    ) -> Tuple[bool, Optional[str]]:
        """
        Create a commit.

        Args:
            working_dir: Repository directory
            message: Commit message
            allow_empty: Allow an initial commit without tracked files
            fallback_identity: Commit-scoped name/email for missing config fields

        Returns:
            Tuple of (success: bool, sha_or_error: Optional[str]).
            On success the second element is the commit SHA.
            On failure the second element is the error message.
        """
        try:
            # Last stop before the message becomes permanent history. Strips
            # only unambiguous generation artifacts; prose is untouched.
            from app.core.git_text import sanitize_commit_text

            args = []
            if fallback_identity:
                for field, fallback in zip(("user.name", "user.email"), fallback_identity):
                    configured = self._run_git(working_dir, ["config", "--get", field], timeout=5)
                    if not configured.stdout.strip():
                        args.extend(["-c", f"{field}={fallback}"])
            args.extend(["commit", "-m", sanitize_commit_text(message)])
            if allow_empty:
                args.append("--allow-empty")
            result = self._run_git(working_dir, args, timeout=30)

            if result.returncode == 0:
                sha_result = self._run_git(working_dir, ["rev-parse", "HEAD"], timeout=5)
                commit_sha = sha_result.stdout.strip()[:12] if sha_result.returncode == 0 else None
                logger.debug(f"Created commit: {commit_sha}")
                self.invalidate_status_cache(working_dir)
                return True, commit_sha

            error_msg = result.stderr.strip() or result.stdout.strip() or f"git commit exited with status {result.returncode}"
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
            if branch is not None and not _is_plain_branch_name(branch):
                return False, f"Invalid branch name: {branch!r}"
            if set_upstream and branch:
                args = ["push", "--set-upstream", remote, _push_refspec(branch)]
            elif branch:
                args = ["push", remote, _push_refspec(branch)]
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
        remote: Optional[str] = None,
        strategy: str = "merge",
        auto_stash: bool = False,
    ) -> Tuple[bool, Optional[str], List[str], Optional[str]]:
        """
        Pull from remote (fetch + merge/rebase).

        Args:
            working_dir: Repository directory
            remote: Explicit remote, or None to use the tracked upstream (else origin)
            strategy: "merge" or "rebase"
            auto_stash: If True, stash uncommitted changes before pull and pop after

        Returns:
            Tuple of (success, error_message, conflicted_files, target)
        """
        stashed = False
        target = None
        try:
            # Get current branch
            branch = self.get_current_branch(working_dir)
            if not branch:
                return False, "Cannot pull: HEAD is detached", [], target

            if remote is None:
                upstream = self._run_git(
                    working_dir,
                    ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
                )
                if upstream.returncode != 0:
                    # Recover a mapped upstream whose tracking ref is missing.
                    # An unmapped but configured upstream is refused below.
                    upstream = self._run_git(
                        working_dir,
                        ["for-each-ref", "--format=%(upstream:short)", f"refs/heads/{branch}"],
                    )
                if upstream.returncode == 0 and upstream.stdout.strip():
                    target = upstream.stdout.strip()
                    # Read the remote separately: remote names can contain slashes,
                    # and the tracked branch need not have the local branch's name.
                    tracked_remote = self._run_git(
                        working_dir, ["config", "--get", f"branch.{branch}.remote"]
                    )
                    if tracked_remote.returncode != 0:
                        return False, f"Cannot determine the remote for {target}", [], target
                    remote = tracked_remote.stdout.strip()
                else:
                    configured_merge = self._run_git(
                        working_dir, ["config", "--get", f"branch.{branch}.merge"]
                    )
                    if configured_merge.returncode != 1:
                        return (
                            False,
                            f"Pull stopped: the configured upstream for {branch} could not be resolved. Check the remote and fetch refspec configuration. Nothing was changed.",
                            [], target,
                        )
                    remote = "origin"
            if remote.startswith("-"):
                return False, f"Invalid remote name: {remote}", [], target
            target = target or f"{remote}/{branch}"

            if not self.fetch(working_dir, remote):
                return False, f"Failed to fetch from '{remote}'", [], target

            # Refuse unrelated histories before rebase can replay the whole branch,
            # and before auto-stash can modify the working tree or stash stack.
            base = self._run_git(working_dir, ["merge-base", "--", "HEAD", target])
            if base.returncode == 1:
                shallow = self._run_git(working_dir, ["rev-parse", "--is-shallow-repository"])
                if shallow.returncode != 0:
                    return False, shallow.stderr.strip() or "Cannot check whether this repository is shallow", [], target
                if shallow.stdout.strip() == "true":
                    return (
                        False,
                        f"Pull stopped: the shared history with {target} isn't in this shallow clone. Run `git fetch --unshallow` (or `--deepen`). Nothing was changed.",
                        [], target,
                    )
                return (
                    False,
                    f"Pull stopped: this branch and {target} share no history (the remote may have changed). Nothing was changed.",
                    [], target,
                )
            if base.returncode != 0:
                return False, base.stderr.strip() or f"Cannot check history with {target}", [], target

            if auto_stash:
                stashed = self.stash_push(working_dir, "auto-stash before pull")

            args = ["rebase" if strategy == "rebase" else "merge", "--", target]

            result = self._run_git(working_dir, args, timeout=120)

            if result.returncode == 0:
                logger.debug(f"Pulled from {target} using {strategy}")
                self.invalidate_status_cache(working_dir)
                # Pop stash after successful pull
                if stashed:
                    pop_ok, pop_err = self.stash_pop(working_dir)
                    if not pop_ok:
                        return True, f"Pull from {target} succeeded but failed to restore stashed changes: {pop_err}", [], target
                return True, None, [], target

            # Check for merge conflicts
            conflicts = self._get_conflict_files(working_dir)
            if conflicts:
                # Auto-abort to keep repo clean, but never claim it worked if it failed.
                abort = self._run_git(working_dir, [args[0], "--abort"], timeout=10)
                files = ", ".join(conflicts)
                if abort.returncode != 0:
                    message = f"Pull from {target} hit conflicts in {files} and could not be aborted: {abort.stderr.strip()}"
                    if stashed:
                        message += ". Your changes are saved in the stash."
                else:
                    message = f"Pull from {target} was stopped due to conflicts in {files}. Nothing was changed."
                    if stashed:
                        pop_ok, pop_err = self.stash_pop(working_dir)
                        if not pop_ok:
                            message = f"Pull from {target} was aborted after conflicts in {files}, but stashed changes could not be restored: {pop_err}"
                logger.warning(message)
                return False, message, conflicts, target

            # Non-conflict error — restore stash
            if stashed:
                self.stash_pop(working_dir)
            error_msg = result.stderr.strip() if result.stderr else "Unknown error"
            logger.warning(f"Pull failed: {error_msg}")
            return False, error_msg, [], target
        except Exception as e:
            # Restore stash on any exception
            if stashed:
                try:
                    self.stash_pop(working_dir)
                except Exception:
                    pass
            logger.error(f"Error pulling: {e}")
            return False, str(e), [], target

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
