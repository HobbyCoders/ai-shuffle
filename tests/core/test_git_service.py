"""
Unit tests for git_service module.

Tests cover:
- Repository introspection (is_git_repo, get_remote_url, get_current_branch, get_default_branch)
- Branch operations (list, create, delete, checkout)
- Worktree operations (list, add, remove)
- Commit graph and status
- Fetch operations
- Error handling and edge cases
"""

import pytest
import subprocess
from unittest.mock import MagicMock, patch, call
from typing import List

from app.core.git_service import GitService, git_service


class TestRunGit:
    """Test the internal _run_git method."""

    def test_run_git_success(self):
        """Should run git command and return result."""
        service = GitService()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["git", "status"],
                returncode=0,
                stdout="On branch main\n",
                stderr=""
            )

            result = service._run_git("/test/repo", ["status"])

            mock_run.assert_called_once_with(
                ["git", "status"],
                cwd="/test/repo",
                capture_output=True,
                text=True,
                timeout=30,
                check=False
            )
            assert result.returncode == 0
            assert result.stdout == "On branch main\n"

    def test_run_git_with_custom_timeout(self):
        """Should respect custom timeout."""
        service = GitService()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["git", "fetch"],
                returncode=0,
                stdout="",
                stderr=""
            )

            service._run_git("/test/repo", ["fetch"], timeout=60)

            mock_run.assert_called_once()
            call_kwargs = mock_run.call_args[1]
            assert call_kwargs["timeout"] == 60

    def test_run_git_with_check_true(self):
        """Should pass check parameter to subprocess."""
        service = GitService()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["git", "status"],
                returncode=0,
                stdout="",
                stderr=""
            )

            service._run_git("/test/repo", ["status"], check=True)

            call_kwargs = mock_run.call_args[1]
            assert call_kwargs["check"] is True

    def test_run_git_timeout_raises(self):
        """Should re-raise TimeoutExpired."""
        service = GitService()

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="git", timeout=30)

            with pytest.raises(subprocess.TimeoutExpired):
                service._run_git("/test/repo", ["status"])

    def test_run_git_called_process_error_raises(self):
        """Should re-raise CalledProcessError."""
        service = GitService()

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(
                returncode=1,
                cmd="git status",
                stderr="error"
            )

            with pytest.raises(subprocess.CalledProcessError):
                service._run_git("/test/repo", ["status"])


class TestIsGitRepo:
    """Test is_git_repo method."""

    def test_is_git_repo_true(self):
        """Should return True for valid git repo."""
        service = GitService()

        with patch.object(service, "_run_git") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["git", "rev-parse", "--git-dir"],
                returncode=0,
                stdout=".git\n",
                stderr=""
            )

            result = service.is_git_repo("/test/repo")

            assert result is True
            mock_run.assert_called_once_with(
                "/test/repo",
                ["rev-parse", "--git-dir"],
                timeout=5
            )

    def test_is_git_repo_false_non_zero_return(self):
        """Should return False for non-git directory."""
        service = GitService()

        with patch.object(service, "_run_git") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["git", "rev-parse", "--git-dir"],
                returncode=128,
                stdout="",
                stderr="fatal: not a git repository"
            )

            result = service.is_git_repo("/not/a/repo")

            assert result is False

    def test_is_git_repo_exception_returns_false(self):
        """Should return False on exception."""
        service = GitService()

        with patch.object(service, "_run_git") as mock_run:
            mock_run.side_effect = Exception("Git not found")

            result = service.is_git_repo("/test/repo")

            assert result is False


class TestGetRemoteUrl:
    """Test get_remote_url method."""

    def test_get_remote_url_success(self):
        """Should return remote URL."""
        service = GitService()

        with patch.object(service, "_run_git") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["git", "remote", "get-url", "origin"],
                returncode=0,
                stdout="https://github.com/user/repo.git\n",
                stderr=""
            )

            result = service.get_remote_url("/test/repo")

            assert result == "https://github.com/user/repo.git"

    def test_get_remote_url_no_remote(self):
        """Should return None if no remote."""
        service = GitService()

        with patch.object(service, "_run_git") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["git", "remote", "get-url", "origin"],
                returncode=2,
                stdout="",
                stderr="fatal: No such remote 'origin'"
            )

            result = service.get_remote_url("/test/repo")

            assert result is None

    def test_get_remote_url_exception(self):
        """Should return None on exception."""
        service = GitService()

        with patch.object(service, "_run_git") as mock_run:
            mock_run.side_effect = Exception("Error")

            result = service.get_remote_url("/test/repo")

            assert result is None


class TestGetCurrentBranch:
    """Test get_current_branch method."""

    def test_get_current_branch_success(self):
        """Should return current branch name."""
        service = GitService()

        with patch.object(service, "_run_git") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["git", "rev-parse", "--abbrev-ref", "HEAD"],
                returncode=0,
                stdout="feature/test\n",
                stderr=""
            )

            result = service.get_current_branch("/test/repo")

            assert result == "feature/test"

    def test_get_current_branch_detached_head(self):
        """Should return None for detached HEAD."""
        service = GitService()

        with patch.object(service, "_run_git") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["git", "rev-parse", "--abbrev-ref", "HEAD"],
                returncode=0,
                stdout="HEAD\n",
                stderr=""
            )

            result = service.get_current_branch("/test/repo")

            assert result is None

    def test_get_current_branch_error(self):
        """Should return None on error."""
        service = GitService()

        with patch.object(service, "_run_git") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["git", "rev-parse", "--abbrev-ref", "HEAD"],
                returncode=128,
                stdout="",
                stderr="fatal: not a git repository"
            )

            result = service.get_current_branch("/test/repo")

            assert result is None

    def test_get_current_branch_exception(self):
        """Should return None on exception."""
        service = GitService()

        with patch.object(service, "_run_git") as mock_run:
            mock_run.side_effect = Exception("Error")

            result = service.get_current_branch("/test/repo")

            assert result is None


class TestGetDefaultBranch:
    """Test get_default_branch method."""

    def test_get_default_branch_from_remote_head(self):
        """Should return branch from remote HEAD."""
        service = GitService()

        with patch.object(service, "_run_git") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["git", "symbolic-ref", "refs/remotes/origin/HEAD", "--short"],
                returncode=0,
                stdout="origin/main\n",
                stderr=""
            )

            result = service.get_default_branch("/test/repo")

            assert result == "main"

    def test_get_default_branch_fallback_to_main(self):
        """Should fallback to main if it exists."""
        service = GitService()

        with patch.object(service, "_run_git") as mock_run:
            def side_effect(working_dir, args, timeout):
                if "symbolic-ref" in args:
                    return subprocess.CompletedProcess(
                        args=args, returncode=128, stdout="", stderr="fatal"
                    )
                elif "main" in args:
                    return subprocess.CompletedProcess(
                        args=args, returncode=0, stdout="abc123\n", stderr=""
                    )
                else:
                    return subprocess.CompletedProcess(
                        args=args, returncode=128, stdout="", stderr="fatal"
                    )

            mock_run.side_effect = side_effect

            result = service.get_default_branch("/test/repo")

            assert result == "main"

    def test_get_default_branch_fallback_to_master(self):
        """Should fallback to master if main doesn't exist."""
        service = GitService()

        with patch.object(service, "_run_git") as mock_run:
            def side_effect(working_dir, args, timeout):
                if "symbolic-ref" in args:
                    return subprocess.CompletedProcess(
                        args=args, returncode=128, stdout="", stderr="fatal"
                    )
                elif "main" in str(args):
                    return subprocess.CompletedProcess(
                        args=args, returncode=128, stdout="", stderr="fatal"
                    )
                elif "master" in str(args):
                    return subprocess.CompletedProcess(
                        args=args, returncode=0, stdout="abc123\n", stderr=""
                    )
                else:
                    return subprocess.CompletedProcess(
                        args=args, returncode=128, stdout="", stderr="fatal"
                    )

            mock_run.side_effect = side_effect

            result = service.get_default_branch("/test/repo")

            assert result == "master"

    def test_get_default_branch_final_fallback(self):
        """Should return 'main' as final fallback."""
        service = GitService()

        with patch.object(service, "_run_git") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=128, stdout="", stderr="fatal"
            )

            result = service.get_default_branch("/test/repo")

            assert result == "main"

    def test_get_default_branch_exception(self):
        """Should return 'main' on exception."""
        service = GitService()

        with patch.object(service, "_run_git") as mock_run:
            mock_run.side_effect = Exception("Error")

            result = service.get_default_branch("/test/repo")

            assert result == "main"


class TestFetch:
    """Test fetch method."""

    def test_fetch_success(self):
        """Should return True on successful fetch."""
        service = GitService()

        with patch.object(service, "_run_git") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["git", "fetch", "origin", "--prune"],
                returncode=0,
                stdout="",
                stderr=""
            )

            result = service.fetch("/test/repo")

            assert result is True
            mock_run.assert_called_once_with(
                "/test/repo",
                ["fetch", "origin", "--prune"],
                timeout=60
            )

    def test_fetch_custom_remote(self):
        """Should use custom remote name."""
        service = GitService()

        with patch.object(service, "_run_git") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["git", "fetch", "upstream", "--prune"],
                returncode=0,
                stdout="",
                stderr=""
            )

            result = service.fetch("/test/repo", remote="upstream")

            assert result is True
            mock_run.assert_called_once_with(
                "/test/repo",
                ["fetch", "upstream", "--prune"],
                timeout=60
            )

    def test_fetch_failure(self):
        """Should return False on fetch failure."""
        service = GitService()

        with patch.object(service, "_run_git") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["git", "fetch", "origin", "--prune"],
                returncode=1,
                stdout="",
                stderr="fatal: unable to access"
            )

            result = service.fetch("/test/repo")

            assert result is False

    def test_fetch_exception(self):
        """Should return False on exception."""
        service = GitService()

        with patch.object(service, "_run_git") as mock_run:
            mock_run.side_effect = Exception("Network error")

            result = service.fetch("/test/repo")

            assert result is False


class TestListBranches:
    """Test list_branches method."""

    def test_list_branches_local_and_remote(self):
        """Should list local and remote branches."""
        service = GitService()

        git_output = (
            "main|abc1234|Initial commit|*|origin/main|[ahead 1, behind 2]\n"
            "feature/test|def5678|Add feature|||[ahead 3]\n"
            "origin/main|abc1234|Initial commit|||\n"
            "origin/develop|ghi9012|Dev commit|||\n"
        )

        with patch.object(service, "_run_git") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=git_output,
                stderr=""
            )

            result = service.list_branches("/test/repo", include_remote=True)

            assert len(result) == 4

            # Check the current branch (main)
            main_branch = next(b for b in result if b["name"] == "main")
            assert main_branch["is_current"] is True
            assert main_branch["is_remote"] is False
            assert main_branch["commit"] == "abc1234"
            assert main_branch["ahead"] == 1
            assert main_branch["behind"] == 2

            # Check feature branch
            feature_branch = next(b for b in result if b["name"] == "feature/test")
            assert feature_branch["is_current"] is False
            assert feature_branch["ahead"] == 3
            assert feature_branch["behind"] == 0

            # Check remote branch
            origin_main = next(b for b in result if b["name"] == "origin/main")
            assert origin_main["is_remote"] is True

    def test_list_branches_local_only(self):
        """Should list only local branches when include_remote=False."""
        service = GitService()

        with patch.object(service, "_run_git") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="main|abc1234|Initial commit|*||\n",
                stderr=""
            )

            result = service.list_branches("/test/repo", include_remote=False)

            # Verify args don't include refs/remotes/
            call_args = mock_run.call_args[0][1]
            assert "refs/heads/" in call_args
            assert "refs/remotes/" not in call_args

    def test_list_branches_skips_origin_head(self):
        """Should skip origin/HEAD reference."""
        service = GitService()

        git_output = (
            "main|abc1234|Initial commit|*||\n"
            "origin/HEAD|abc1234|Initial commit|||\n"
        )

        with patch.object(service, "_run_git") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=git_output,
                stderr=""
            )

            result = service.list_branches("/test/repo")

            branch_names = [b["name"] for b in result]
            assert "origin/HEAD" not in branch_names

    def test_list_branches_empty_output(self):
        """Should return empty list for empty output."""
        service = GitService()

        with patch.object(service, "_run_git") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="",
                stderr=""
            )

            result = service.list_branches("/test/repo")

            assert result == []

    def test_list_branches_failure(self):
        """Should return empty list on failure."""
        service = GitService()

        with patch.object(service, "_run_git") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=128,
                stdout="",
                stderr="fatal: not a git repository"
            )

            result = service.list_branches("/test/repo")

            assert result == []

    def test_list_branches_exception(self):
        """Should return empty list on exception."""
        service = GitService()

        with patch.object(service, "_run_git") as mock_run:
            mock_run.side_effect = Exception("Error")

            result = service.list_branches("/test/repo")

            assert result == []

    def test_list_branches_malformed_line(self):
        """Should skip malformed lines."""
        service = GitService()

        git_output = (
            "main|abc1234|Initial commit|*||\n"
            "incomplete\n"  # Malformed line
            "feature|def5678|Another commit|||\n"
        )

        with patch.object(service, "_run_git") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=git_output,
                stderr=""
            )

            result = service.list_branches("/test/repo")

            assert len(result) == 2

    def test_list_branches_behind_only(self):
        """Should parse behind-only track info."""
        service = GitService()

        git_output = "main|abc1234|Commit message|*|origin/main|[behind 5]\n"

        with patch.object(service, "_run_git") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=git_output,
                stderr=""
            )

            result = service.list_branches("/test/repo")

            assert result[0]["ahead"] == 0
            assert result[0]["behind"] == 5

    def test_list_branches_malformed_ahead_track_info(self):
        """Should handle malformed ahead track info gracefully."""
        service = GitService()

        # Malformed track info - missing number after "ahead"
        git_output = "main|abc1234|Commit message|*|origin/main|[ahead ]\n"

        with patch.object(service, "_run_git") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=git_output,
                stderr=""
            )

            result = service.list_branches("/test/repo")

            # Should default to 0 when parsing fails
            assert result[0]["ahead"] == 0
            assert result[0]["behind"] == 0

    def test_list_branches_malformed_behind_track_info(self):
        """Should handle malformed behind track info gracefully."""
        service = GitService()

        # Malformed track info - non-numeric value after "behind"
        git_output = "main|abc1234|Commit message|*|origin/main|[behind xyz]\n"

        with patch.object(service, "_run_git") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=git_output,
                stderr=""
            )

            result = service.list_branches("/test/repo")

            # Should default to 0 when parsing fails
            assert result[0]["ahead"] == 0
            assert result[0]["behind"] == 0


class TestCreateBranch:
    """Test create_branch method."""

    def test_create_branch_success(self):
        """Should create branch successfully."""
        service = GitService()

        with patch.object(service, "_run_git") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["git", "branch", "new-feature"],
                returncode=0,
                stdout="",
                stderr=""
            )

            result = service.create_branch("/test/repo", "new-feature")

            assert result is True
            mock_run.assert_called_once_with(
                "/test/repo",
                ["branch", "new-feature"],
                timeout=10
            )

    def test_create_branch_with_start_point(self):
        """Should create branch from start point."""
        service = GitService()

        with patch.object(service, "_run_git") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["git", "branch", "new-feature", "develop"],
                returncode=0,
                stdout="",
                stderr=""
            )

            result = service.create_branch("/test/repo", "new-feature", start_point="develop")

            assert result is True
            mock_run.assert_called_once_with(
                "/test/repo",
                ["branch", "new-feature", "develop"],
                timeout=10
            )

    def test_create_branch_failure(self):
        """Should return False on failure."""
        service = GitService()

        with patch.object(service, "_run_git") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=128,
                stdout="",
                stderr="fatal: branch 'existing' already exists"
            )

            result = service.create_branch("/test/repo", "existing")

            assert result is False

    def test_create_branch_exception(self):
        """Should return False on exception."""
        service = GitService()

        with patch.object(service, "_run_git") as mock_run:
            mock_run.side_effect = Exception("Error")

            result = service.create_branch("/test/repo", "new-feature")

            assert result is False


class TestDeleteBranch:
    """Test delete_branch method."""

    def test_delete_branch_success(self):
        """Should delete branch successfully."""
        service = GitService()

        with patch.object(service, "_run_git") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["git", "branch", "-d", "old-feature"],
                returncode=0,
                stdout="",
                stderr=""
            )

            result = service.delete_branch("/test/repo", "old-feature")

            assert result is True
            mock_run.assert_called_once_with(
                "/test/repo",
                ["branch", "-d", "old-feature"],
                timeout=10
            )

    def test_delete_branch_force(self):
        """Should force delete branch."""
        service = GitService()

        with patch.object(service, "_run_git") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["git", "branch", "-D", "unmerged-feature"],
                returncode=0,
                stdout="",
                stderr=""
            )

            result = service.delete_branch("/test/repo", "unmerged-feature", force=True)

            assert result is True
            mock_run.assert_called_once_with(
                "/test/repo",
                ["branch", "-D", "unmerged-feature"],
                timeout=10
            )

    def test_delete_branch_failure(self):
        """Should return False on failure."""
        service = GitService()

        with patch.object(service, "_run_git") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=1,
                stdout="",
                stderr="error: branch 'main' is not fully merged"
            )

            result = service.delete_branch("/test/repo", "main")

            assert result is False

    def test_delete_branch_exception(self):
        """Should return False on exception."""
        service = GitService()

        with patch.object(service, "_run_git") as mock_run:
            mock_run.side_effect = Exception("Error")

            result = service.delete_branch("/test/repo", "old-feature")

            assert result is False


class TestCheckout:
    """Test checkout method."""

    def test_checkout_branch_success(self):
        """Should checkout branch successfully."""
        service = GitService()

        with patch.object(service, "_run_git") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["git", "checkout", "develop"],
                returncode=0,
                stdout="",
                stderr="Switched to branch 'develop'"
            )

            result = service.checkout("/test/repo", "develop")

            assert result is True
            mock_run.assert_called_once_with(
                "/test/repo",
                ["checkout", "develop"],
                timeout=30
            )

    def test_checkout_commit_success(self):
        """Should checkout commit successfully."""
        service = GitService()

        with patch.object(service, "_run_git") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["git", "checkout", "abc1234"],
                returncode=0,
                stdout="",
                stderr="HEAD is now at abc1234"
            )

            result = service.checkout("/test/repo", "abc1234")

            assert result is True

    def test_checkout_failure(self):
        """Should return False on failure."""
        service = GitService()

        with patch.object(service, "_run_git") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=1,
                stdout="",
                stderr="error: pathspec 'nonexistent' did not match any file(s)"
            )

            result = service.checkout("/test/repo", "nonexistent")

            assert result is False

    def test_checkout_exception(self):
        """Should return False on exception."""
        service = GitService()

        with patch.object(service, "_run_git") as mock_run:
            mock_run.side_effect = Exception("Error")

            result = service.checkout("/test/repo", "develop")

            assert result is False


class TestListWorktrees:
    """Test list_worktrees method."""

    def test_list_worktrees_success(self):
        """Should list worktrees with all properties."""
        service = GitService()

        git_output = (
            "worktree /path/to/main\n"
            "HEAD abc1234567890\n"
            "branch refs/heads/main\n"
            "\n"
            "worktree /path/to/feature\n"
            "HEAD def5678901234\n"
            "branch refs/heads/feature\n"
            "\n"
            "worktree /path/to/detached\n"
            "HEAD ghi9012345678\n"
            "detached\n"
            "\n"
        )

        with patch.object(service, "_run_git") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=git_output,
                stderr=""
            )
            with patch("os.path.abspath") as mock_abspath:
                mock_abspath.side_effect = lambda x: x

                result = service.list_worktrees("/path/to/main")

                assert len(result) == 3

                # Check main worktree
                main_wt = result[0]
                assert main_wt["path"] == "/path/to/main"
                assert main_wt["head"] == "abc1234567890"
                assert main_wt["branch"] == "main"
                assert main_wt["is_main"] is True
                assert main_wt["is_detached"] is False

                # Check feature worktree
                feature_wt = result[1]
                assert feature_wt["branch"] == "feature"
                assert feature_wt["is_main"] is False

                # Check detached worktree
                detached_wt = result[2]
                assert detached_wt["is_detached"] is True
                assert detached_wt["branch"] is None

    def test_list_worktrees_bare_repo(self):
        """Should handle bare repository worktree."""
        service = GitService()

        git_output = (
            "worktree /path/to/bare.git\n"
            "bare\n"
            "\n"
        )

        with patch.object(service, "_run_git") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=git_output,
                stderr=""
            )
            with patch("os.path.abspath") as mock_abspath:
                mock_abspath.side_effect = lambda x: x

                result = service.list_worktrees("/path/to/bare.git")

                assert len(result) == 1
                assert result[0]["is_bare"] is True

    def test_list_worktrees_failure(self):
        """Should return empty list on failure."""
        service = GitService()

        with patch.object(service, "_run_git") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=128,
                stdout="",
                stderr="fatal: not a git repository"
            )

            result = service.list_worktrees("/not/a/repo")

            assert result == []

    def test_list_worktrees_exception(self):
        """Should return empty list on exception."""
        service = GitService()

        with patch.object(service, "_run_git") as mock_run:
            mock_run.side_effect = Exception("Error")

            result = service.list_worktrees("/test/repo")

            assert result == []

    def test_list_worktrees_branch_without_refs_heads(self):
        """Should handle branch refs without refs/heads/ prefix."""
        service = GitService()

        git_output = (
            "worktree /path/to/repo\n"
            "HEAD abc1234\n"
            "branch custom-branch\n"
            "\n"
        )

        with patch.object(service, "_run_git") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=git_output,
                stderr=""
            )
            with patch("os.path.abspath") as mock_abspath:
                mock_abspath.side_effect = lambda x: x

                result = service.list_worktrees("/path/to/repo")

                assert result[0]["branch"] == "custom-branch"

    def test_list_worktrees_no_trailing_blank_line(self):
        """Should handle output that doesn't end with a blank line."""
        service = GitService()

        # No trailing blank line after the last worktree
        git_output = (
            "worktree /path/to/main\n"
            "HEAD abc1234567890\n"
            "branch refs/heads/main"  # No trailing newline
        )

        with patch.object(service, "_run_git") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=git_output,
                stderr=""
            )
            with patch("os.path.abspath") as mock_abspath:
                mock_abspath.side_effect = lambda x: x

                result = service.list_worktrees("/path/to/main")

                assert len(result) == 1
                assert result[0]["path"] == "/path/to/main"
                assert result[0]["branch"] == "main"


class TestAddWorktree:
    """Test add_worktree method."""

    def test_add_worktree_existing_branch(self):
        """Should add worktree for existing branch."""
        service = GitService()

        with patch.object(service, "_run_git") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="Preparing worktree",
                stderr=""
            )

            success, error = service.add_worktree(
                "/main/repo",
                "/path/to/worktree",
                "feature"
            )

            assert success is True
            assert error is None
            mock_run.assert_called_once_with(
                "/main/repo",
                ["worktree", "add", "/path/to/worktree", "feature"],
                timeout=30
            )

    def test_add_worktree_new_branch(self):
        """Should add worktree with new branch."""
        service = GitService()

        with patch.object(service, "_run_git") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="",
                stderr=""
            )

            success, error = service.add_worktree(
                "/main/repo",
                "/path/to/worktree",
                "new-feature",
                new_branch=True
            )

            assert success is True
            assert error is None
            mock_run.assert_called_once_with(
                "/main/repo",
                ["worktree", "add", "-b", "new-feature", "/path/to/worktree"],
                timeout=30
            )

    def test_add_worktree_new_branch_with_base(self):
        """Should add worktree with new branch from base."""
        service = GitService()

        with patch.object(service, "_run_git") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="",
                stderr=""
            )

            success, error = service.add_worktree(
                "/main/repo",
                "/path/to/worktree",
                "new-feature",
                new_branch=True,
                base_branch="develop"
            )

            assert success is True
            mock_run.assert_called_once_with(
                "/main/repo",
                ["worktree", "add", "-b", "new-feature", "/path/to/worktree", "develop"],
                timeout=30
            )

    def test_add_worktree_failure(self):
        """Should return error message on failure."""
        service = GitService()

        with patch.object(service, "_run_git") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=128,
                stdout="",
                stderr="fatal: '/path/to/worktree' already exists"
            )

            success, error = service.add_worktree(
                "/main/repo",
                "/path/to/worktree",
                "feature"
            )

            assert success is False
            assert "already exists" in error

    def test_add_worktree_failure_empty_stderr(self):
        """Should return 'Unknown git error' when stderr is empty."""
        service = GitService()

        with patch.object(service, "_run_git") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=128,
                stdout="",
                stderr=""
            )

            success, error = service.add_worktree(
                "/main/repo",
                "/path/to/worktree",
                "feature"
            )

            assert success is False
            assert error == "Unknown git error"

    def test_add_worktree_exception(self):
        """Should return error message on exception."""
        service = GitService()

        with patch.object(service, "_run_git") as mock_run:
            mock_run.side_effect = Exception("Disk full")

            success, error = service.add_worktree(
                "/main/repo",
                "/path/to/worktree",
                "feature"
            )

            assert success is False
            assert "Disk full" in error


class TestRemoveWorktree:
    """Test remove_worktree method."""

    def test_remove_worktree_success(self):
        """Should remove worktree successfully."""
        service = GitService()

        with patch.object(service, "_run_git") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="",
                stderr=""
            )

            result = service.remove_worktree("/main/repo", "/path/to/worktree")

            assert result is True
            mock_run.assert_called_once_with(
                "/main/repo",
                ["worktree", "remove", "/path/to/worktree"],
                timeout=30
            )

    def test_remove_worktree_force(self):
        """Should force remove worktree."""
        service = GitService()

        with patch.object(service, "_run_git") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="",
                stderr=""
            )

            result = service.remove_worktree(
                "/main/repo",
                "/path/to/worktree",
                force=True
            )

            assert result is True
            mock_run.assert_called_once_with(
                "/main/repo",
                ["worktree", "remove", "--force", "/path/to/worktree"],
                timeout=30
            )

    def test_remove_worktree_failure(self):
        """Should return False on failure."""
        service = GitService()

        with patch.object(service, "_run_git") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=128,
                stdout="",
                stderr="fatal: worktree has changes"
            )

            result = service.remove_worktree("/main/repo", "/path/to/worktree")

            assert result is False

    def test_remove_worktree_exception(self):
        """Should return False on exception."""
        service = GitService()

        with patch.object(service, "_run_git") as mock_run:
            mock_run.side_effect = Exception("Error")

            result = service.remove_worktree("/main/repo", "/path/to/worktree")

            assert result is False


class TestGetCommitGraph:
    """Test get_commit_graph method."""

    def test_get_commit_graph_success(self):
        """Should return commit graph data."""
        service = GitService()

        git_output = (
            "abc1234567890|abc1234|Initial commit|John Doe|john@example.com|2024-01-01T12:00:00+00:00||HEAD -> main, origin/main\n"
            "def5678901234|def5678|Add feature|Jane Doe|jane@example.com|2024-01-02T12:00:00+00:00|abc1234567890|feature\n"
            "ghi9012345678|ghi9012|Merge branch|John Doe|john@example.com|2024-01-03T12:00:00+00:00|abc1234567890 def5678901234|\n"
        )

        with patch.object(service, "_run_git") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=git_output,
                stderr=""
            )

            result = service.get_commit_graph("/test/repo", limit=50)

            assert len(result) == 3

            # Check first commit
            first_commit = result[0]
            assert first_commit["sha"] == "abc1234567890"
            assert first_commit["short_sha"] == "abc1234"
            assert first_commit["message"] == "Initial commit"
            assert first_commit["author"] == "John Doe"
            assert first_commit["author_email"] == "john@example.com"
            assert first_commit["parents"] == []
            assert "HEAD -> main" in first_commit["refs"]
            assert "origin/main" in first_commit["refs"]

            # Check merge commit
            merge_commit = result[2]
            assert len(merge_commit["parents"]) == 2

    def test_get_commit_graph_specific_branch(self):
        """Should get commits for specific branch."""
        service = GitService()

        with patch.object(service, "_run_git") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="abc123|abc|Message|Author|email@test.com|2024-01-01T12:00:00+00:00||\n",
                stderr=""
            )

            result = service.get_commit_graph("/test/repo", branch="feature")

            call_args = mock_run.call_args[0][1]
            assert "feature" in call_args
            assert "--all" not in call_args

    def test_get_commit_graph_all_branches(self):
        """Should use --all when no branch specified."""
        service = GitService()

        with patch.object(service, "_run_git") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="",
                stderr=""
            )

            service.get_commit_graph("/test/repo")

            call_args = mock_run.call_args[0][1]
            assert "--all" in call_args

    def test_get_commit_graph_empty(self):
        """Should return empty list for empty repo."""
        service = GitService()

        with patch.object(service, "_run_git") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="",
                stderr=""
            )

            result = service.get_commit_graph("/test/repo")

            assert result == []

    def test_get_commit_graph_failure(self):
        """Should return empty list on failure."""
        service = GitService()

        with patch.object(service, "_run_git") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=128,
                stdout="",
                stderr="fatal: not a git repository"
            )

            result = service.get_commit_graph("/test/repo")

            assert result == []

    def test_get_commit_graph_exception(self):
        """Should return empty list on exception."""
        service = GitService()

        with patch.object(service, "_run_git") as mock_run:
            mock_run.side_effect = Exception("Error")

            result = service.get_commit_graph("/test/repo")

            assert result == []

    def test_get_commit_graph_malformed_line(self):
        """Should skip malformed lines."""
        service = GitService()

        git_output = (
            "abc123|abc|Message|Author|email@test.com|2024-01-01T12:00:00+00:00||\n"
            "incomplete\n"
            "def456|def|Message2|Author2|email2@test.com|2024-01-02T12:00:00+00:00|abc123|\n"
        )

        with patch.object(service, "_run_git") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=git_output,
                stderr=""
            )

            result = service.get_commit_graph("/test/repo")

            assert len(result) == 2


class TestGetStatus:
    """Test get_status method."""

    def test_get_status_not_git_repo(self):
        """Should return default status for non-git repo."""
        service = GitService()

        with patch.object(service, "is_git_repo") as mock_is_repo:
            mock_is_repo.return_value = False

            result = service.get_status("/not/a/repo")

            assert result["is_git_repo"] is False
            assert result["current_branch"] is None
            assert result["is_clean"] is True

    def test_get_status_clean_repo(self):
        """Should return clean status for clean repo."""
        service = GitService()

        with patch.object(service, "is_git_repo") as mock_is_repo:
            mock_is_repo.return_value = True
            with patch.object(service, "get_remote_url") as mock_remote:
                mock_remote.return_value = "https://github.com/user/repo.git"
                with patch.object(service, "_run_git") as mock_run:
                    def side_effect(working_dir, args, timeout=30):
                        if args == ["rev-parse", "--abbrev-ref", "HEAD"]:
                            return subprocess.CompletedProcess(
                                args=args, returncode=0, stdout="main\n", stderr=""
                            )
                        elif args == ["rev-parse", "HEAD"]:
                            return subprocess.CompletedProcess(
                                args=args, returncode=0, stdout="abc1234567890\n", stderr=""
                            )
                        elif "rev-list" in args:
                            return subprocess.CompletedProcess(
                                args=args, returncode=0, stdout="2\t3\n", stderr=""
                            )
                        elif "status" in args:
                            return subprocess.CompletedProcess(
                                args=args, returncode=0, stdout="", stderr=""
                            )
                        return subprocess.CompletedProcess(
                            args=args, returncode=0, stdout="", stderr=""
                        )

                    mock_run.side_effect = side_effect

                    result = service.get_status("/test/repo")

                    assert result["is_git_repo"] is True
                    assert result["current_branch"] == "main"
                    assert result["is_detached"] is False
                    assert result["head_commit"] == "abc123456789"
                    assert result["remote_url"] == "https://github.com/user/repo.git"
                    assert result["is_clean"] is True
                    assert result["ahead"] == 2
                    assert result["behind"] == 3

    def test_get_status_detached_head(self):
        """Should detect detached HEAD state."""
        service = GitService()

        with patch.object(service, "is_git_repo") as mock_is_repo:
            mock_is_repo.return_value = True
            with patch.object(service, "get_remote_url") as mock_remote:
                mock_remote.return_value = None
                with patch.object(service, "_run_git") as mock_run:
                    def side_effect(working_dir, args, timeout=30):
                        if args == ["rev-parse", "--abbrev-ref", "HEAD"]:
                            return subprocess.CompletedProcess(
                                args=args, returncode=0, stdout="HEAD\n", stderr=""
                            )
                        elif args == ["rev-parse", "HEAD"]:
                            return subprocess.CompletedProcess(
                                args=args, returncode=0, stdout="abc123\n", stderr=""
                            )
                        elif "status" in args:
                            return subprocess.CompletedProcess(
                                args=args, returncode=0, stdout="", stderr=""
                            )
                        return subprocess.CompletedProcess(
                            args=args, returncode=0, stdout="", stderr=""
                        )

                    mock_run.side_effect = side_effect

                    result = service.get_status("/test/repo")

                    assert result["is_detached"] is True
                    assert result["current_branch"] is None

    def test_get_status_with_changes(self):
        """Should detect staged, modified, and untracked files."""
        service = GitService()

        status_output = (
            "M  staged_file.py\n"
            " M modified_file.py\n"
            "?? untracked_file.py\n"
            "A  new_staged_file.py\n"
            "D  deleted_file.py\n"
            "R  renamed_file.py\n"
            "C  copied_file.py\n"
        )

        with patch.object(service, "is_git_repo") as mock_is_repo:
            mock_is_repo.return_value = True
            with patch.object(service, "get_remote_url") as mock_remote:
                mock_remote.return_value = None
                with patch.object(service, "_run_git") as mock_run:
                    def side_effect(working_dir, args, timeout=30):
                        if args == ["rev-parse", "--abbrev-ref", "HEAD"]:
                            return subprocess.CompletedProcess(
                                args=args, returncode=0, stdout="main\n", stderr=""
                            )
                        elif args == ["rev-parse", "HEAD"]:
                            return subprocess.CompletedProcess(
                                args=args, returncode=0, stdout="abc123\n", stderr=""
                            )
                        elif "rev-list" in args:
                            return subprocess.CompletedProcess(
                                args=args, returncode=0, stdout="0\t0\n", stderr=""
                            )
                        elif "status" in args:
                            return subprocess.CompletedProcess(
                                args=args, returncode=0, stdout=status_output, stderr=""
                            )
                        return subprocess.CompletedProcess(
                            args=args, returncode=0, stdout="", stderr=""
                        )

                    mock_run.side_effect = side_effect

                    result = service.get_status("/test/repo")

                    assert result["is_clean"] is False
                    assert len(result["staged"]) == 5  # M, A, D, R, C
                    assert len(result["modified"]) == 1
                    assert len(result["untracked"]) == 1
                    assert "modified_file.py" in result["modified"]
                    assert "untracked_file.py" in result["untracked"]

    def test_get_status_with_conflicts(self):
        """Should detect merge conflicts."""
        service = GitService()

        status_output = (
            "UU conflicted_file.py\n"
            "AA both_added.py\n"
            "DD both_deleted.py\n"
        )

        with patch.object(service, "is_git_repo") as mock_is_repo:
            mock_is_repo.return_value = True
            with patch.object(service, "get_remote_url") as mock_remote:
                mock_remote.return_value = None
                with patch.object(service, "_run_git") as mock_run:
                    def side_effect(working_dir, args, timeout=30):
                        if args == ["rev-parse", "--abbrev-ref", "HEAD"]:
                            return subprocess.CompletedProcess(
                                args=args, returncode=0, stdout="main\n", stderr=""
                            )
                        elif args == ["rev-parse", "HEAD"]:
                            return subprocess.CompletedProcess(
                                args=args, returncode=0, stdout="abc123\n", stderr=""
                            )
                        elif "rev-list" in args:
                            return subprocess.CompletedProcess(
                                args=args, returncode=0, stdout="0\t0\n", stderr=""
                            )
                        elif "status" in args:
                            return subprocess.CompletedProcess(
                                args=args, returncode=0, stdout=status_output, stderr=""
                            )
                        return subprocess.CompletedProcess(
                            args=args, returncode=0, stdout="", stderr=""
                        )

                    mock_run.side_effect = side_effect

                    result = service.get_status("/test/repo")

                    assert result["is_clean"] is False
                    assert len(result["conflicts"]) == 3
                    assert "conflicted_file.py" in result["conflicts"]
                    assert "both_added.py" in result["conflicts"]
                    assert "both_deleted.py" in result["conflicts"]

    def test_get_status_exception(self):
        """Should handle exceptions gracefully."""
        service = GitService()

        with patch.object(service, "is_git_repo") as mock_is_repo:
            mock_is_repo.return_value = True
            with patch.object(service, "get_remote_url") as mock_remote:
                mock_remote.return_value = None
                with patch.object(service, "_run_git") as mock_run:
                    mock_run.side_effect = Exception("Unexpected error")

                    result = service.get_status("/test/repo")

                    # Should return partial status
                    assert result["is_git_repo"] is True

    def test_get_status_short_lines_skipped(self):
        """Should skip lines with less than 3 characters."""
        service = GitService()

        status_output = (
            "M  staged_file.py\n"
            "X\n"  # Too short
            " \n"  # Too short
            " M modified_file.py\n"
        )

        with patch.object(service, "is_git_repo") as mock_is_repo:
            mock_is_repo.return_value = True
            with patch.object(service, "get_remote_url") as mock_remote:
                mock_remote.return_value = None
                with patch.object(service, "_run_git") as mock_run:
                    def side_effect(working_dir, args, timeout=30):
                        if args == ["rev-parse", "--abbrev-ref", "HEAD"]:
                            return subprocess.CompletedProcess(
                                args=args, returncode=0, stdout="main\n", stderr=""
                            )
                        elif args == ["rev-parse", "HEAD"]:
                            return subprocess.CompletedProcess(
                                args=args, returncode=0, stdout="abc123\n", stderr=""
                            )
                        elif "rev-list" in args:
                            return subprocess.CompletedProcess(
                                args=args, returncode=0, stdout="0\t0\n", stderr=""
                            )
                        elif "status" in args:
                            return subprocess.CompletedProcess(
                                args=args, returncode=0, stdout=status_output, stderr=""
                            )
                        return subprocess.CompletedProcess(
                            args=args, returncode=0, stdout="", stderr=""
                        )

                    mock_run.side_effect = side_effect

                    result = service.get_status("/test/repo")

                    assert len(result["staged"]) == 1
                    assert len(result["modified"]) == 1

    def test_get_status_no_upstream(self):
        """Should handle missing upstream gracefully."""
        service = GitService()

        with patch.object(service, "is_git_repo") as mock_is_repo:
            mock_is_repo.return_value = True
            with patch.object(service, "get_remote_url") as mock_remote:
                mock_remote.return_value = None
                with patch.object(service, "_run_git") as mock_run:
                    def side_effect(working_dir, args, timeout=30):
                        if args == ["rev-parse", "--abbrev-ref", "HEAD"]:
                            return subprocess.CompletedProcess(
                                args=args, returncode=0, stdout="main\n", stderr=""
                            )
                        elif args == ["rev-parse", "HEAD"]:
                            return subprocess.CompletedProcess(
                                args=args, returncode=0, stdout="abc123\n", stderr=""
                            )
                        elif "rev-list" in args:
                            # No upstream configured
                            return subprocess.CompletedProcess(
                                args=args, returncode=128, stdout="", stderr="fatal: no upstream"
                            )
                        elif "status" in args:
                            return subprocess.CompletedProcess(
                                args=args, returncode=0, stdout="", stderr=""
                            )
                        return subprocess.CompletedProcess(
                            args=args, returncode=0, stdout="", stderr=""
                        )

                    mock_run.side_effect = side_effect

                    result = service.get_status("/test/repo")

                    assert result["ahead"] == 0
                    assert result["behind"] == 0


class TestGlobalInstance:
    """Test the global git_service instance."""

    def test_global_instance_exists(self):
        """Should have a global git_service instance."""
        assert git_service is not None
        assert isinstance(git_service, GitService)


class TestBranchSorting:
    """Test branch sorting in list_branches."""

    def test_branches_sorted_current_first(self):
        """Current branch should appear first."""
        service = GitService()

        git_output = (
            "develop|abc123|Commit||||\n"
            "main|def456|Commit|*|||\n"
            "feature|ghi789|Commit||||\n"
        )

        with patch.object(service, "_run_git") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=git_output,
                stderr=""
            )

            result = service.list_branches("/test/repo", include_remote=False)

            assert result[0]["name"] == "main"
            assert result[0]["is_current"] is True

    def test_branches_sorted_local_before_remote(self):
        """Local branches should appear before remote branches."""
        service = GitService()

        git_output = (
            "origin/main|abc123|Commit||||\n"
            "main|def456|Commit||||\n"
        )

        with patch.object(service, "_run_git") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=git_output,
                stderr=""
            )

            result = service.list_branches("/test/repo")

            local_idx = next(i for i, b in enumerate(result) if b["name"] == "main")
            remote_idx = next(i for i, b in enumerate(result) if b["name"] == "origin/main")
            assert local_idx < remote_idx
