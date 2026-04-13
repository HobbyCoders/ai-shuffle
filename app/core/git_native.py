"""
Git Native Reader — pygit2 (libgit2) implementation of read-only git operations.

Replaces subprocess-based git CLI calls in GitService with fast, in-process
reads via pygit2. Every method returns the EXACT same shape as the
corresponding GitService method.

Thread safety: each method opens a fresh pygit2.Repository handle (~0.1ms)
because pygit2.Repository is NOT thread-safe across threads. This is still
10-50x faster than spawning a git subprocess.
"""

import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional pygit2 import
# ---------------------------------------------------------------------------
try:
    import pygit2

    HAS_PYGIT2 = True
except ImportError:
    pygit2 = None  # type: ignore[assignment]
    HAS_PYGIT2 = False

# ---------------------------------------------------------------------------
# Status flag bitmask constants (only defined when pygit2 is available)
# ---------------------------------------------------------------------------
if HAS_PYGIT2:
    _INDEX_FLAGS = (
        pygit2.GIT_STATUS_INDEX_NEW
        | pygit2.GIT_STATUS_INDEX_MODIFIED
        | pygit2.GIT_STATUS_INDEX_DELETED
        | pygit2.GIT_STATUS_INDEX_RENAMED
        | pygit2.GIT_STATUS_INDEX_TYPECHANGE
    )
    _WT_MODIFIED_FLAGS = (
        pygit2.GIT_STATUS_WT_MODIFIED
        | pygit2.GIT_STATUS_WT_DELETED
        | pygit2.GIT_STATUS_WT_TYPECHANGE
    )
else:
    _INDEX_FLAGS = 0
    _WT_MODIFIED_FLAGS = 0

# ---------------------------------------------------------------------------
# Module-level tracking for fsmonitor configuration
# ---------------------------------------------------------------------------
_fsmonitor_configured: set = set()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ensure_fsmonitor(repo: "pygit2.Repository") -> None:
    """Enable core.fsmonitor if not already set for this repo."""
    repo_path = os.path.realpath(repo.path)
    if repo_path in _fsmonitor_configured:
        return

    try:
        try:
            value = repo.config["core.fsmonitor"]
            if value == "true":
                logger.debug("fsmonitor already enabled for %s", repo_path)
                _fsmonitor_configured.add(repo_path)
                return
        except KeyError:
            pass

        repo.config["core.fsmonitor"] = "true"
        logger.info("Enabled core.fsmonitor for %s", repo_path)
        _fsmonitor_configured.add(repo_path)
    except Exception as exc:
        logger.debug("Could not configure fsmonitor for %s: %s", repo_path, exc)
        # Still mark as configured to avoid repeated attempts
        _fsmonitor_configured.add(repo_path)


def _get_repo(working_dir: str) -> Optional["pygit2.Repository"]:
    """Open a fresh pygit2.Repository for *working_dir*.

    Returns None if pygit2 is unavailable or the path is not a git repo.
    """
    if not HAS_PYGIT2:
        return None
    try:
        repo = pygit2.Repository(os.path.realpath(working_dir))
        _ensure_fsmonitor(repo)
        return repo
    except Exception:
        return None


def _index_status_char(flags: int) -> str:
    """Map pygit2 index status flags to porcelain v1 single-character codes."""
    if flags & pygit2.GIT_STATUS_INDEX_NEW:
        return "A"
    if flags & pygit2.GIT_STATUS_INDEX_MODIFIED:
        return "M"
    if flags & pygit2.GIT_STATUS_INDEX_DELETED:
        return "D"
    if flags & pygit2.GIT_STATUS_INDEX_RENAMED:
        return "R"
    if flags & pygit2.GIT_STATUS_INDEX_TYPECHANGE:
        return "T"
    return " "


def _resolve_oid(repo: "pygit2.Repository", ref_str: str) -> Optional["pygit2.Oid"]:
    """Resolve a ref string to an Oid, trying references then revparse."""
    try:
        ref = repo.references.get(ref_str)
        if ref is not None:
            return ref.resolve().target
    except Exception:
        pass
    try:
        obj = repo.revparse_single(ref_str)
        return obj.id
    except Exception:
        return None


def _format_timestamp(epoch: int, offset_minutes: int) -> str:
    """Format a commit timestamp as ISO 8601 with timezone offset."""
    tz = timezone(timedelta(minutes=offset_minutes))
    dt = datetime.fromtimestamp(epoch, tz=tz)
    return dt.isoformat()


def _staged_diff(repo: "pygit2.Repository") -> Optional["pygit2.Diff"]:
    """Compute the staged (index vs HEAD tree) diff.

    Handles unborn HEAD (no commits yet) by diffing index against an empty tree.
    Always calls repo.index.read() first to refresh from disk.
    """
    try:
        repo.index.read()
        if repo.head_is_unborn:
            return repo.index.diff_to_tree()
        tree = repo.head.peel(pygit2.Tree)
        return repo.index.diff_to_tree(tree)
    except Exception:
        return None


def _unstaged_diff(repo: "pygit2.Repository") -> Optional["pygit2.Diff"]:
    """Compute the unstaged (workdir vs index) diff."""
    try:
        repo.index.read()
        return repo.diff()
    except Exception:
        return None


def _both_diffs(
    repo: "pygit2.Repository",
) -> Tuple[Optional["pygit2.Diff"], Optional["pygit2.Diff"]]:
    """Compute staged and unstaged diffs in one shot (single index read).

    Returns (staged_diff, unstaged_diff).
    """
    try:
        repo.index.read()
    except Exception:
        return (None, None)

    staged = None
    try:
        if repo.head_is_unborn:
            staged = repo.index.diff_to_tree()
        else:
            staged = repo.index.diff_to_tree(repo.head.peel(pygit2.Tree))
    except Exception:
        pass

    unstaged = None
    try:
        unstaged = repo.diff()
    except Exception:
        pass

    return (staged, unstaged)


def _is_reparse_point(path: str) -> bool:
    """Check if *path* is a Windows reparse point (junction/symlink).

    Returns False on non-Windows platforms or on any error.
    """
    if os.name != "nt":
        return False
    try:
        import ctypes

        FILE_ATTRIBUTE_REPARSE_POINT = 0x400
        attrs = ctypes.windll.kernel32.GetFileAttributesW(path)  # type: ignore[union-attr]
        if attrs == -1:  # INVALID_FILE_ATTRIBUTES
            return False
        return bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT)
    except Exception:
        return False


def _is_phantom_deletion(repo: "pygit2.Repository", patch: "pygit2.Patch") -> bool:
    """Return True if *patch* is a false-positive deletion.

    libgit2 on Windows reports files inside junction / reparse-point
    directories as deleted even when they exist on disk.  This helper
    detects that case so callers can skip the patch.
    """
    if patch.delta.status != pygit2.GIT_DELTA_DELETED:
        return False
    workdir = repo.workdir
    if workdir is None:
        return False
    full = os.path.join(workdir, patch.delta.old_file.path)
    return os.path.exists(full)


# ---------------------------------------------------------------------------
# GitNativeReader — all read-only git operations via pygit2
# ---------------------------------------------------------------------------


class GitNativeReader:
    """Fast, in-process git read operations using pygit2 (libgit2 bindings).

    Every public method mirrors a GitService method and returns the exact
    same data shape so the two implementations are interchangeable for reads.
    """

    # ------------------------------------------------------------------
    # Method 1: is_git_repo
    # ------------------------------------------------------------------

    def is_git_repo(self, working_dir: str) -> bool:
        """Check if *working_dir* is a git repository root.

        Replaces GitService.is_git_repo (git rev-parse --show-toplevel).
        Returns True only when *working_dir* IS the repo root, not a subdir.
        """
        try:
            repo = _get_repo(working_dir)
            if repo is None:
                return False
            workdir = repo.workdir
            if workdir is None:
                return False
            repo_root = os.path.realpath(workdir.rstrip("/\\"))
            target = os.path.realpath(working_dir)
            return repo_root == target
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Method 2: get_remote_url
    # ------------------------------------------------------------------

    def get_remote_url(self, working_dir: str) -> Optional[str]:
        """Get the URL of the 'origin' remote.

        Replaces GitService.get_remote_url (git remote get-url origin).
        """
        try:
            repo = _get_repo(working_dir)
            if repo is None:
                return None
            return repo.remotes["origin"].url
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Method 3: get_current_branch
    # ------------------------------------------------------------------

    def get_current_branch(self, working_dir: str) -> Optional[str]:
        """Get the current branch name, or None if detached/unborn.

        Replaces GitService.get_current_branch (git rev-parse --abbrev-ref HEAD).
        """
        try:
            repo = _get_repo(working_dir)
            if repo is None:
                return None
            if repo.head_is_unborn or repo.head_is_detached:
                return None
            return repo.head.shorthand
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Method 4: get_default_branch
    # ------------------------------------------------------------------

    def get_default_branch(self, working_dir: str) -> str:
        """Get the default branch name (usually main or master).

        Replaces GitService.get_default_branch (git symbolic-ref refs/remotes/origin/HEAD).
        """
        try:
            repo = _get_repo(working_dir)
            if repo is None:
                return "main"

            # Try refs/remotes/origin/HEAD
            try:
                ref = repo.references.get("refs/remotes/origin/HEAD")
                if ref is not None:
                    resolved = ref.resolve()
                    # e.g. refs/remotes/origin/main -> "main"
                    name = resolved.shorthand  # "origin/main"
                    return name.split("/", 1)[-1] if "/" in name else name
            except Exception:
                pass

            # Fallback: check common branch names
            for branch in ("main", "master"):
                try:
                    if repo.references.get(f"refs/heads/{branch}") is not None:
                        return branch
                except Exception:
                    pass

            return "main"
        except Exception:
            return "main"

    # ------------------------------------------------------------------
    # Method 5: get_status
    # ------------------------------------------------------------------

    def get_status(self, working_dir: str) -> Dict[str, Any]:
        """Get full repository status.

        Replaces GitService.get_status (git status --porcelain + rev-parse + rev-list).
        Returns the same dict shape with all expected keys.
        """
        status: Dict[str, Any] = {
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
            "conflicts": [],
        }

        try:
            repo = _get_repo(working_dir)
            if repo is None:
                return status

            # Verify this is actually the repo root
            workdir = repo.workdir
            if workdir is None:
                return status
            repo_root = os.path.realpath(workdir.rstrip("/\\"))
            if repo_root != os.path.realpath(working_dir):
                return status

            status["is_git_repo"] = True

            # Branch / HEAD info
            if repo.head_is_unborn:
                pass  # no branch, no commit
            elif repo.head_is_detached:
                status["is_detached"] = True
                status["head_commit"] = str(repo.head.target)[:12]
            else:
                status["current_branch"] = repo.head.shorthand
                status["head_commit"] = str(repo.head.target)[:12]

            # Remote URL
            try:
                status["remote_url"] = repo.remotes["origin"].url
            except Exception:
                pass

            # File status
            workdir_path = repo.workdir or working_dir
            file_statuses = repo.status()
            for filepath, flags in file_statuses.items():
                if flags & pygit2.GIT_STATUS_CONFLICTED:
                    status["conflicts"].append(filepath)
                    continue

                # libgit2 on Windows may report WT_DELETED for files
                # inside junction/reparse-point directories that actually
                # exist.  Verify before treating as a real deletion.
                if flags & pygit2.GIT_STATUS_WT_DELETED:
                    full = os.path.join(workdir_path, filepath)
                    if os.path.exists(full):
                        # False positive — file exists on disk.  Skip the
                        # worktree flags entirely (git CLI also ignores it).
                        if flags & _INDEX_FLAGS:
                            status["staged"].append({
                                "file": filepath,
                                "status": _index_status_char(flags),
                            })
                        continue

                if flags & _INDEX_FLAGS:
                    status["staged"].append({
                        "file": filepath,
                        "status": _index_status_char(flags),
                    })

                if flags & _WT_MODIFIED_FLAGS:
                    status["modified"].append(filepath)

                if flags & pygit2.GIT_STATUS_WT_NEW:
                    # Skip reparse-point directories (junctions/symlinks)
                    # that libgit2 reports as untracked but git CLI ignores.
                    full = os.path.join(workdir_path, filepath)
                    if os.path.isdir(full) and _is_reparse_point(full):
                        continue
                    status["untracked"].append(filepath)

            # Ahead/behind relative to default branch (matching GitService behavior)
            if status["current_branch"] and not repo.head_is_unborn:
                default_branch = self.get_default_branch(working_dir)
                local_oid = repo.head.target

                for ref_name in (f"refs/remotes/origin/{default_branch}", f"refs/heads/{default_branch}"):
                    remote_oid = _resolve_oid(repo, ref_name)
                    if remote_oid is not None:
                        try:
                            ahead, behind = repo.ahead_behind(local_oid, remote_oid)
                            status["ahead"] = ahead
                            status["behind"] = behind
                        except Exception:
                            pass
                        break

            status["is_clean"] = (
                not status["staged"]
                and not status["modified"]
                and not status["untracked"]
                and not status["conflicts"]
            )

        except Exception as exc:
            logger.error("Error getting git status via pygit2: %s", exc)

        return status

    # ------------------------------------------------------------------
    # Method 6: get_diff_stats
    # ------------------------------------------------------------------

    def get_diff_stats(
        self,
        working_dir: str,
        include_unstaged: bool = True,
    ) -> Dict[str, Any]:
        """Get aggregate diff statistics (additions, deletions, files_changed).

        Replaces GitService.get_diff_stats (git diff --numstat).
        """
        result: Dict[str, Any] = {"additions": 0, "deletions": 0, "files_changed": 0}

        try:
            repo = _get_repo(working_dir)
            if repo is None:
                return result

            total_add = 0
            total_del = 0
            files: set = set()

            # Single index read for both diffs
            sdiff, udiff = _both_diffs(repo)

            # Staged diff (no rename detection needed for aggregate stats)
            if sdiff is not None:
                for patch in sdiff:
                    if patch.delta.is_binary:
                        files.add(patch.delta.new_file.path)
                        continue
                    _, additions, deletions = patch.line_stats
                    total_add += additions
                    total_del += deletions
                    files.add(patch.delta.new_file.path)

            # Unstaged diff
            if include_unstaged and udiff is not None:
                for patch in udiff:
                    if _is_phantom_deletion(repo, patch):
                        continue
                    if patch.delta.is_binary:
                        files.add(patch.delta.new_file.path)
                        continue
                    _, additions, deletions = patch.line_stats
                    total_add += additions
                    total_del += deletions
                    files.add(patch.delta.new_file.path)

            result["additions"] = total_add
            result["deletions"] = total_del
            result["files_changed"] = len(files)

        except Exception as exc:
            logger.error("Error getting diff stats via pygit2: %s", exc)

        return result

    # ------------------------------------------------------------------
    # Method 7: get_per_file_diff_stats
    # ------------------------------------------------------------------

    def get_per_file_diff_stats(self, working_dir: str) -> List[Dict[str, Any]]:
        """Get per-file diff statistics.

        Replaces GitService.get_per_file_diff_stats (git diff --numstat for staged + unstaged).
        """
        files: List[Dict[str, Any]] = []

        try:
            repo = _get_repo(working_dir)
            if repo is None:
                return files

            # Single index read for both diffs
            sdiff, udiff = _both_diffs(repo)

            # Staged
            if sdiff is not None:
                for patch in sdiff:
                    is_binary = patch.delta.is_binary
                    if is_binary:
                        additions = 0
                        deletions = 0
                    else:
                        _, additions, deletions = patch.line_stats
                    files.append({
                        "file": patch.delta.new_file.path,
                        "additions": additions,
                        "deletions": deletions,
                        "is_binary": is_binary,
                        "staged": True,
                    })

            # Unstaged
            if udiff is not None:
                for patch in udiff:
                    if _is_phantom_deletion(repo, patch):
                        continue
                    is_binary = patch.delta.is_binary
                    if is_binary:
                        additions = 0
                        deletions = 0
                    else:
                        _, additions, deletions = patch.line_stats
                    files.append({
                        "file": patch.delta.new_file.path,
                        "additions": additions,
                        "deletions": deletions,
                        "is_binary": is_binary,
                        "staged": False,
                    })

        except Exception as exc:
            logger.error("Error getting per-file diff stats via pygit2: %s", exc)

        return files

    # ------------------------------------------------------------------
    # Method 8: get_file_diff
    # ------------------------------------------------------------------

    def get_file_diff(
        self,
        working_dir: str,
        file_path: str,
        staged: bool = False,
        max_chars: int = 50000,
    ) -> Dict[str, Any]:
        """Get unified diff for a single file.

        Replaces GitService.get_file_diff (git diff / git diff --staged / synthesized diff).
        Includes path traversal protection.
        """
        result_data: Dict[str, Any] = {
            "file": file_path,
            "diff": "",
            "is_binary": False,
            "truncated": False,
        }

        try:
            # Path traversal protection
            real_wd = os.path.realpath(working_dir)
            full_path = os.path.realpath(os.path.join(working_dir, file_path))
            if not full_path.startswith(real_wd + os.sep) and full_path != real_wd:
                result_data["diff"] = "Error: path traversal blocked"
                return result_data

            repo = _get_repo(working_dir)
            if repo is None:
                return result_data

            # Check if untracked
            try:
                file_flags = repo.status_file(file_path)
            except Exception:
                file_flags = 0

            is_untracked = bool(file_flags & pygit2.GIT_STATUS_WT_NEW)

            if is_untracked:
                return self._diff_untracked_file(
                    working_dir, file_path, full_path, max_chars, result_data
                )

            # Tracked file — get the appropriate diff
            if staged:
                diff = _staged_diff(repo)
            else:
                diff = _unstaged_diff(repo)

            if diff is None:
                return result_data

            diff.find_similar(flags=pygit2.GIT_DIFF_FIND_RENAMES)

            # Find the patch for our file
            for patch in diff:
                if _is_phantom_deletion(repo, patch):
                    continue
                delta = patch.delta
                if delta.new_file.path == file_path or delta.old_file.path == file_path:
                    if delta.is_binary:
                        result_data["is_binary"] = True
                        result_data["diff"] = "Binary file (not displayable)"
                        return result_data

                    diff_text = patch.text or ""
                    if len(diff_text) > max_chars:
                        diff_text = diff_text[:max_chars] + "\n\n[... diff truncated ...]"
                        result_data["truncated"] = True
                    result_data["diff"] = diff_text
                    return result_data

        except Exception as exc:
            logger.error("Error getting file diff via pygit2 for %s: %s", file_path, exc)
            result_data["diff"] = f"Error: {exc}"

        return result_data

    def _diff_untracked_file(
        self,
        working_dir: str,
        file_path: str,
        full_path: str,
        max_chars: int,
        result_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Synthesize a unified diff for an untracked file."""
        try:
            # Read file to check for binary content
            with open(full_path, "rb") as f:
                head = f.read(8192)

            if b"\x00" in head:
                result_data["is_binary"] = True
                result_data["diff"] = "Binary file (not displayable)"
                return result_data

            # Read full file as text
            with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()

            lines = content.splitlines(True)
            line_count = len(lines)

            # Build unified diff
            parts = [
                f"diff --git a/{file_path} b/{file_path}\n",
                "new file mode 100644\n",
                "--- /dev/null\n",
                f"+++ b/{file_path}\n",
                f"@@ -0,0 +1,{line_count} @@\n",
            ]
            for line in lines:
                stripped = line.rstrip("\n\r")
                parts.append(f"+{stripped}\n")

            diff_text = "".join(parts)

            if len(diff_text) > max_chars:
                diff_text = diff_text[:max_chars] + "\n\n[... diff truncated ...]"
                result_data["truncated"] = True

            result_data["diff"] = diff_text

        except Exception as exc:
            logger.error("Error synthesizing diff for untracked file %s: %s", file_path, exc)
            result_data["diff"] = f"Error: {exc}"

        return result_data

    # ------------------------------------------------------------------
    # Method 9: get_diff_text
    # ------------------------------------------------------------------

    def get_diff_text(
        self,
        working_dir: str,
        include_unstaged: bool = True,
        max_chars: int = 8000,
    ) -> str:
        """Get combined diff text for AI commit message generation.

        Replaces GitService.get_diff_text (git diff --staged + git diff).
        """
        try:
            repo = _get_repo(working_dir)
            if repo is None:
                return ""

            parts: List[str] = []

            # Staged diff text
            sdiff = _staged_diff(repo)
            if sdiff is not None:
                sdiff.find_similar(flags=pygit2.GIT_DIFF_FIND_RENAMES)
                patch_text = sdiff.patch
                if patch_text and patch_text.strip():
                    parts.append(patch_text.strip())

            # Unstaged diff text (iterate patches to filter phantom deletions)
            if include_unstaged:
                udiff = _unstaged_diff(repo)
                if udiff is not None:
                    udiff.find_similar(flags=pygit2.GIT_DIFF_FIND_RENAMES)
                    for patch in udiff:
                        if _is_phantom_deletion(repo, patch):
                            continue
                        ptxt = patch.text
                        if ptxt and ptxt.strip():
                            parts.append(ptxt.strip())

            diff_text = "\n".join(parts)

            if len(diff_text) > max_chars:
                diff_text = diff_text[:max_chars] + "\n\n[... diff truncated ...]"

            return diff_text

        except Exception as exc:
            logger.error("Error getting diff text via pygit2: %s", exc)
            return ""

    # ------------------------------------------------------------------
    # Method 10: list_branches
    # ------------------------------------------------------------------

    def list_branches(
        self,
        working_dir: str,
        include_remote: bool = True,
    ) -> List[Dict[str, Any]]:
        """List all branches with ahead/behind info.

        Replaces GitService.list_branches (git for-each-ref + rev-list).
        """
        try:
            repo = _get_repo(working_dir)
            if repo is None:
                return []

            # Determine current branch
            current_branch: Optional[str] = None
            if not repo.head_is_unborn and not repo.head_is_detached:
                current_branch = repo.head.shorthand

            branches: List[Dict[str, Any]] = []

            # Local branches
            for branch_name in repo.branches.local:
                try:
                    branch = repo.branches.local[branch_name]
                    commit = branch.peel(pygit2.Commit)
                    is_current = branch_name == current_branch

                    ahead = 0
                    behind = 0
                    effective_upstream: Optional[str] = None

                    try:
                        upstream = branch.upstream
                        if upstream is not None:
                            upstream_shorthand = upstream.shorthand
                            # Check if upstream points to a different branch name
                            if not upstream_shorthand.endswith(f"/{branch_name}"):
                                # Upstream is a different branch (e.g., origin/main for feature).
                                # Check if own remote counterpart exists.
                                remote_name = upstream_shorthand.split("/")[0] if "/" in upstream_shorthand else "origin"
                                own_remote_ref = f"{remote_name}/{branch_name}"

                                own_remote_oid = _resolve_oid(repo, f"refs/remotes/{own_remote_ref}")
                                if own_remote_oid is not None:
                                    # Own remote counterpart exists
                                    effective_upstream = own_remote_ref
                                    try:
                                        ahead, behind = repo.ahead_behind(commit.id, own_remote_oid)
                                    except Exception:
                                        ahead, behind = 0, 0
                                else:
                                    # Not pushed yet
                                    ahead = 0
                                    behind = 0
                                    effective_upstream = None
                            else:
                                # Upstream matches branch name — normal ahead/behind
                                effective_upstream = upstream_shorthand
                                try:
                                    upstream_commit = upstream.peel(pygit2.Commit)
                                    ahead, behind = repo.ahead_behind(commit.id, upstream_commit.id)
                                except Exception:
                                    ahead, behind = 0, 0
                    except Exception:
                        pass

                    branches.append({
                        "name": branch_name,
                        "is_current": is_current,
                        "is_remote": False,
                        "commit": str(commit.id)[:7],
                        "commit_message": commit.message.split("\n", 1)[0][:100],
                        "upstream": effective_upstream,
                        "ahead": ahead,
                        "behind": behind,
                    })
                except Exception as exc:
                    logger.debug("Error processing local branch %s: %s", branch_name, exc)
                    continue

            # Remote branches
            if include_remote:
                for branch_name in repo.branches.remote:
                    if branch_name == "origin/HEAD" or branch_name == "origin":
                        continue
                    try:
                        branch = repo.branches.remote[branch_name]
                        commit = branch.peel(pygit2.Commit)
                        branches.append({
                            "name": branch_name,
                            "is_current": False,
                            "is_remote": True,
                            "commit": str(commit.id)[:7],
                            "commit_message": commit.message.split("\n", 1)[0][:100],
                            "upstream": None,
                            "ahead": 0,
                            "behind": 0,
                        })
                    except Exception as exc:
                        logger.debug("Error processing remote branch %s: %s", branch_name, exc)
                        continue

            # Sort: current first, then local, then remote, all case-insensitive
            branches.sort(key=lambda b: (
                not b["is_current"],
                b["is_remote"],
                b["name"].lower(),
            ))

            return branches

        except Exception as exc:
            logger.error("Error listing branches via pygit2: %s", exc)
            return []

    # ------------------------------------------------------------------
    # Method 11: get_commit_graph
    # ------------------------------------------------------------------

    def get_commit_graph(
        self,
        working_dir: str,
        limit: int = 100,
        branch: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Get commit graph data for visualization.

        Replaces GitService.get_commit_graph (git log --format=... --all).
        """
        try:
            repo = _get_repo(working_dir)
            if repo is None:
                return []

            if repo.head_is_unborn:
                return []

            # Build ref_map: {oid_hex: [ref_names]}
            ref_map: Dict[str, List[str]] = {}

            current_branch: Optional[str] = None
            if not repo.head_is_detached and not repo.head_is_unborn:
                current_branch = repo.head.shorthand

            for ref_name in repo.references:
                try:
                    ref = repo.references[ref_name]
                    resolved = ref.resolve()
                    oid_hex = str(resolved.target)

                    if ref_name.startswith("refs/heads/"):
                        display = ref_name[len("refs/heads/"):]
                    elif ref_name.startswith("refs/remotes/"):
                        display = ref_name[len("refs/remotes/"):]
                    elif ref_name.startswith("refs/tags/"):
                        display = "tag: " + ref_name[len("refs/tags/"):]
                    else:
                        continue

                    ref_map.setdefault(oid_hex, []).append(display)
                except Exception:
                    continue

            # Add "HEAD -> branch" decoration
            if current_branch is not None:
                head_oid = str(repo.head.target)
                head_decoration = f"HEAD -> {current_branch}"
                existing = ref_map.get(head_oid, [])
                # Remove plain branch name if present — will be replaced by HEAD -> branch
                if current_branch in existing:
                    existing.remove(current_branch)
                existing.insert(0, head_decoration)
                ref_map[head_oid] = existing

            # Set up the walker
            walker = repo.walk(repo.head.target, pygit2.GIT_SORT_TIME)

            if branch:
                # Walk specific branch
                branch_oid = _resolve_oid(repo, f"refs/heads/{branch}")
                if branch_oid is not None:
                    walker = repo.walk(branch_oid, pygit2.GIT_SORT_TIME)
                else:
                    return []
            else:
                # Walk all refs
                for ref_name in repo.references:
                    try:
                        ref = repo.references[ref_name]
                        resolved = ref.resolve()
                        # Only push commit-ish refs
                        try:
                            obj = repo.get(resolved.target)
                            if obj is not None and obj.type == pygit2.GIT_OBJ_COMMIT:
                                walker.push(resolved.target)
                        except Exception:
                            pass
                    except Exception:
                        continue

            commits: List[Dict[str, Any]] = []
            count = 0
            for commit in walker:
                if count >= limit:
                    break

                oid_hex = str(commit.id)
                refs = ref_map.get(oid_hex, [])

                commits.append({
                    "sha": oid_hex,
                    "short_sha": oid_hex[:7],
                    "message": commit.message.split("\n", 1)[0] if commit.message else "",
                    "author": commit.author.name,
                    "author_email": commit.author.email,
                    "timestamp": _format_timestamp(commit.author.time, commit.author.offset),
                    "parents": [str(p) for p in commit.parent_ids],
                    "refs": refs,
                })
                count += 1

            return commits

        except Exception as exc:
            logger.error("Error getting commit graph via pygit2: %s", exc)
            return []

    # ------------------------------------------------------------------
    # Method 12: get_ahead_behind_counts
    # ------------------------------------------------------------------

    def get_ahead_behind_counts(self, working_dir: str) -> Tuple[int, int]:
        """Get ahead/behind counts relative to upstream tracking branch.

        Replaces GitService.get_ahead_behind_counts (git rev-list --count --left-right @{u}...HEAD).
        """
        try:
            repo = _get_repo(working_dir)
            if repo is None:
                return (0, 0)

            if repo.head_is_unborn or repo.head_is_detached:
                return (0, 0)

            branch_name = repo.head.shorthand
            branch = repo.branches.local[branch_name]
            upstream = branch.upstream
            if upstream is None:
                return (0, 0)

            head_oid = repo.head.target
            upstream_oid = upstream.peel(pygit2.Commit).id
            ahead, behind = repo.ahead_behind(head_oid, upstream_oid)
            return (ahead, behind)

        except Exception:
            return (0, 0)

    # ------------------------------------------------------------------
    # Method 13: get_branch_log
    # ------------------------------------------------------------------

    def get_branch_log(
        self,
        working_dir: str,
        base_branch: str = "main",
        max_commits: int = 50,
    ) -> Tuple[str, str]:
        """Get commit log and diff text between base branch and HEAD.

        Replaces GitService.get_branch_log (git log base..HEAD --oneline + git diff base..HEAD --stat).
        Returns (commit_log_text, diff_text) for AI PR description generation.
        """
        commit_log = ""
        diff_text = ""

        try:
            repo = _get_repo(working_dir)
            if repo is None:
                return (commit_log, diff_text)

            if repo.head_is_unborn:
                return (commit_log, diff_text)

            head_oid = repo.head.target

            # Find the base branch oid
            base_oid = None
            for ref_name in (f"refs/remotes/origin/{base_branch}", f"refs/heads/{base_branch}"):
                base_oid = _resolve_oid(repo, ref_name)
                if base_oid is not None:
                    break

            if base_oid is None:
                return (commit_log, diff_text)

            # Find merge base
            try:
                merge_base_oid = repo.merge_base(head_oid, base_oid)
            except Exception:
                return (commit_log, diff_text)

            if merge_base_oid is None:
                return (commit_log, diff_text)

            # Walk commits from HEAD to merge base
            walker = repo.walk(head_oid, pygit2.GIT_SORT_TIME)
            walker.hide(merge_base_oid)

            log_lines: List[str] = []
            count = 0
            for commit in walker:
                if count >= max_commits:
                    break
                short_sha = str(commit.id)[:7]
                subject = commit.message.split("\n", 1)[0] if commit.message else ""
                log_lines.append(f"{short_sha} {subject}")
                count += 1

            commit_log = "\n".join(log_lines)

            # Get diff between merge base and HEAD
            try:
                merge_base_commit = repo.get(merge_base_oid)
                head_commit = repo.get(head_oid)
                if merge_base_commit is not None and head_commit is not None:
                    merge_base_tree = merge_base_commit.peel(pygit2.Tree)
                    head_tree = head_commit.peel(pygit2.Tree)
                    diff = repo.diff(merge_base_tree, head_tree)
                    diff.find_similar(flags=pygit2.GIT_DIFF_FIND_RENAMES)
                    diff_text = diff.patch or ""
                    # Truncate reasonably for AI consumption
                    max_diff = 50000
                    if len(diff_text) > max_diff:
                        diff_text = diff_text[:max_diff] + "\n\n[... diff truncated ...]"
            except Exception as exc:
                logger.debug("Error computing branch diff: %s", exc)

        except Exception as exc:
            logger.error("Error getting branch log via pygit2: %s", exc)

        return (commit_log, diff_text)

    # ------------------------------------------------------------------
    # Cache management
    # ------------------------------------------------------------------

    @staticmethod
    def invalidate_repo(working_dir: str) -> None:
        """Remove a repo path from the fsmonitor tracking set.

        Called after structural mutations (worktree add/remove, init, clone)
        so that fsmonitor configuration is re-checked on next access.
        With the fresh-per-call Repository strategy there is no handle cache
        to clear, but this ensures fsmonitor state stays consistent.
        """
        real_path = os.path.realpath(working_dir)
        to_discard = {p for p in _fsmonitor_configured if p.startswith(real_path)}
        _fsmonitor_configured.difference_update(to_discard)
