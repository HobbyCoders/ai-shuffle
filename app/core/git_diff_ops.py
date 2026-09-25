"""
Git Diff Operations Mixin

Provides diff-related git operations: diff stats, file diffs,
per-file diff stats, diff text, and branch log.
"""

import logging
import os
from typing import Any, Dict, List, Tuple

from app.core.git_cache import (
    _git_cache,
    _native_reader,
    DIFF_STATS_CACHE_TTL,
)

logger = logging.getLogger(__name__)


def _parse_numstat_z(output: str) -> List[Tuple[str, int, int, bool]]:
    """Parse ``git diff --numstat -z`` into (path, additions, deletions, is_binary).

    ``-z`` keeps paths verbatim (no C-quoting of non-ASCII names). A rename is
    reported as ``add<TAB>del<TAB>`` followed by the old and new paths as two
    further NUL-terminated fields; the new path is the one git status reports.
    """
    rows: List[Tuple[str, int, int, bool]] = []
    fields = iter(output.split("\0"))
    for field in fields:
        if not field:
            continue
        parts = field.split("\t", 2)
        if len(parts) < 3:
            continue
        added, deleted, path = parts
        if not path:
            next(fields, None)  # rename source
            path = next(fields, "")
            if not path:
                continue
        is_binary = added == "-" and deleted == "-"
        rows.append((
            path,
            0 if added == "-" else int(added),
            0 if deleted == "-" else int(deleted),
            is_binary,
        ))
    return rows


class GitDiffMixin:
    """Mixin providing diff operations for GitService."""

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
        cached = _git_cache.get(cache_key, DIFF_STATS_CACHE_TTL)
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

            numstat_runs = [["diff", "--staged", "--numstat", "-z"]]
            if include_unstaged:
                numstat_runs.append(["diff", "--numstat", "-z"])
            for args in numstat_runs:
                result = self._run_git(working_dir, args, timeout=10)
                if result.returncode != 0:
                    continue
                for path, added, deleted, _binary in _parse_numstat_z(result.stdout):
                    total_additions += added
                    total_deletions += deleted
                    files.add(path)

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
        cached = _git_cache.get(cache_key, DIFF_STATS_CACHE_TTL)
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
            for staged, args in (
                (True, ["diff", "--staged", "--numstat", "-z"]),
                (False, ["diff", "--numstat", "-z"]),
            ):
                result = self._run_git(working_dir, args, timeout=10)
                if result.returncode != 0:
                    continue
                for path, added, deleted, is_binary in _parse_numstat_z(result.stdout):
                    files.append({
                        "file": path,
                        "additions": added,
                        "deletions": deleted,
                        "is_binary": is_binary,
                        "staged": staged,
                    })

            _git_cache.put(cache_key, files)
        except Exception as e:
            logger.error(f"Error getting per-file diff stats: {e}")

        return files

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

        # "<base>..HEAD" sits in revision position, where git still parses a
        # leading "-" as an option: a base of "--output=<path>" would make
        # both commands below write a file. No real branch starts with "-".
        if base_branch.startswith("-"):
            logger.warning("Refusing option-like base branch: %s", base_branch[:50])
            return (commit_log, diff_stat)

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
