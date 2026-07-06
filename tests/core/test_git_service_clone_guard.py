"""
Security tests for GitService.clone_repo URL hardening (Phase 1.3).

clone_repo must reject:
- clone URLs that begin with '-' (option/flag injection)
- remote-helper schemes ext:: / file:: (arbitrary command execution)

and must terminate git option parsing with '--' before the positional URL.
"""

import pytest
from unittest.mock import MagicMock, patch

from app.core.git_service import GitService


@pytest.fixture
def git():
    return GitService()


class TestCloneRepoUrlGuard:
    """Malicious clone URLs must fail closed before any subprocess runs."""

    @pytest.mark.parametrize("bad_url", [
        "--upload-pack=x",
        "--upload-pack=touch /tmp/pwn",
        "-oProxyCommand=evil",
        "ext::sh -c id",
        "EXT::sh -c id",          # case-insensitive
        "file::/etc/passwd",
        "FILE::/etc/passwd",      # case-insensitive
    ])
    def test_malicious_url_rejected_before_subprocess(self, git, bad_url):
        # A rejected URL returns (False, reason) and never reaches _run_git.
        with patch.object(git, "_run_git") as mock_run:
            success, error = git.clone_repo("/tmp/work", bad_url)
        assert success is False
        assert error  # a non-empty rejection reason
        mock_run.assert_not_called()

    def test_benign_url_passes_guard_and_uses_dash_dash(self, git):
        """A normal https URL is cloned with '--' before the positional URL."""
        fake_result = MagicMock()
        fake_result.returncode = 0
        with patch.object(git, "_run_git", return_value=fake_result) as mock_run:
            success, error = git.clone_repo("/tmp/work", "https://github.com/o/r.git")

        assert success is True
        assert error is None
        # Inspect the args passed to git.
        called_args = mock_run.call_args[0]
        git_args = called_args[1]
        assert git_args[0] == "clone"
        assert "--" in git_args
        # '--' must come immediately before the URL positional.
        dash_idx = git_args.index("--")
        assert git_args[dash_idx + 1] == "https://github.com/o/r.git"
        assert git_args[-1] == "."

    def test_ssh_url_is_allowed(self, git):
        """SSH-form URLs (git@host:org/repo) must not be rejected."""
        fake_result = MagicMock()
        fake_result.returncode = 0
        with patch.object(git, "_run_git", return_value=fake_result) as mock_run:
            success, _ = git.clone_repo("/tmp/work", "git@github.com:org/repo.git")
        assert success is True
        mock_run.assert_called_once()
