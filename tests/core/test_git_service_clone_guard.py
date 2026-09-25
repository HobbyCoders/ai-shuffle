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
        "fd::3",                  # any remote helper
        "persistent-https::x",
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


class TestCloneRepoTerminalPrompt:
    """clone must disable git's interactive credential prompt so a
    credential-requiring remote fails fast instead of blocking until timeout."""

    def test_clone_sets_git_terminal_prompt_zero(self, git):
        fake_result = MagicMock()
        fake_result.returncode = 0
        with patch.object(git, "_run_git", return_value=fake_result) as mock_run:
            git.clone_repo("/tmp/work", "https://github.com/o/r.git")

        env = mock_run.call_args.kwargs["env"]
        assert env is not None
        assert env["GIT_TERMINAL_PROMPT"] == "0"

    def test_clone_env_preserves_inherited_vars(self, git):
        """The prompt-disable is merged onto os.environ, not a bare replacement —
        inherited vars (PATH, git config env, etc.) must survive."""
        fake_result = MagicMock()
        fake_result.returncode = 0
        with patch.dict("os.environ", {"AI_SHUFFLE_CLONE_ENV_PROBE": "keep-me"}, clear=False):
            with patch.object(git, "_run_git", return_value=fake_result) as mock_run:
                git.clone_repo("/tmp/work", "https://github.com/o/r.git")

        env = mock_run.call_args.kwargs["env"]
        assert env["AI_SHUFFLE_CLONE_ENV_PROBE"] == "keep-me"
        assert env["GIT_TERMINAL_PROMPT"] == "0"


class TestCloneCredentialRedaction:
    """A clone URL with userinfo never reaches logs or the client error text."""

    URL = "https://alice:s3cr3t@git.example.com/o/r.git"
    TOKEN_URL = "https://ghp_tok3n@github.com/o/r.git"

    def test_failed_clone_error_and_logs_are_redacted(self, git, caplog):
        fake_result = MagicMock(returncode=128, stderr=(
            f"Cloning into '.'...\nfatal: unable to access '{self.URL}': 403\n"
            f"fatal: repository '{self.TOKEN_URL}' not found\n"
        ))
        with caplog.at_level("DEBUG", logger="app.core.git_service"), \
             patch.object(git, "_run_git", return_value=fake_result):
            success, error = git.clone_repo("/tmp/work", self.URL)

        assert success is False
        assert "s3cr3t" not in error and "ghp_tok3n" not in error
        assert "https://***@git.example.com/o/r.git" in error
        assert "s3cr3t" not in caplog.text and "ghp_tok3n" not in caplog.text

    def test_successful_clone_debug_log_is_redacted(self, git, caplog):
        with caplog.at_level("DEBUG", logger="app.core.git_service"), \
             patch.object(git, "_run_git", return_value=MagicMock(returncode=0)):
            assert git.clone_repo("/tmp/work", self.URL) == (True, None)
        assert "Cloned repository from https://***@git.example.com/o/r.git" in caplog.text
        assert "s3cr3t" not in caplog.text

    def test_run_git_failure_log_is_redacted(self, git, caplog, tmp_path):
        import subprocess

        err = subprocess.CalledProcessError(128, ["git"], stderr=f"fatal: '{self.URL}' denied")
        with caplog.at_level("WARNING", logger="app.core.git_service"), \
             patch("app.core.git_service.subprocess.run", side_effect=err), \
             pytest.raises(subprocess.CalledProcessError):
            git._run_git(str(tmp_path), ["clone", "--", self.URL, "."], check=True)
        assert "s3cr3t" not in caplog.text
        assert "https://***@git.example.com" in caplog.text


def test_redact_url_credentials_shapes():
    from app.core.scrub import redact_url_credentials

    assert redact_url_credentials("https://u:p@h/r.git") == "https://***@h/r.git"
    assert redact_url_credentials("x https://tok@h:8443/r and ssh://git:pw@h/r") == (
        "x https://***@h:8443/r and ssh://***@h/r"
    )
    # Nothing to redact: ssh-style remotes, host:port, plain text.
    for text in ("git@github.com:o/r.git", "http://localhost:8080/api", "no url here", ""):
        assert redact_url_credentials(text) == text


class TestClassifyCloneUrl:
    """How the clone route reads a source: remote, local (with the path git
    opens) or refused. Mirrors git's own URL / scp-like / path split."""

    @pytest.mark.parametrize("url", [
        "https://github.com/o/r.git",
        "http://git.example.com/r.git",
        "HTTPS://github.com/o/r.git",
        "https://tok@github.com/o/r.git",
        "ssh://git@github.com/o/r.git",
        "git+ssh://git@github.com/o/r.git",
        "git@github.com:o/r.git",
        "deploy@git.example.com:/srv/r.git",
        "github.com:o/r.git",
        "git@[::1]:r.git",
    ])
    def test_remote(self, url):
        from app.core.git_service import classify_clone_url
        assert classify_clone_url(url) == ("remote", None)

    @pytest.mark.parametrize("url, path", [
        ("/srv/repos/r.git", "/srv/repos/r.git"),
        ("../other/r.git", "../other/r.git"),
        ("r.git", "r.git"),
        ("./a:b", "./a:b"),                  # '/' before ':' -> a path, as in git
        ("file:///srv/repos/r.git", "/srv/repos/r.git"),
        ("FILE:///srv/r.git", "/srv/r.git"),
        # R10-9: Windows drive / UNC / device paths are local on every
        # platform (git's has_dos_drive_prefix), never scp-like host "C".
        ("C:\\Users\\me\\repo", "C:\\Users\\me\\repo"),
        ("C:/Users/me/repo", "C:/Users/me/repo"),
        ("d:\\", "d:\\"),
        ("z:/", "z:/"),
        ("\\\\server\\share\\repo", "\\\\server\\share\\repo"),
        ("\\\\?\\C:\\repo", "\\\\?\\C:\\repo"),
    ])
    def test_local(self, url, path):
        from app.core.git_service import classify_clone_url
        assert classify_clone_url(url) == ("local", path)

    @pytest.mark.parametrize("url", [
        "", "   ", "-oProxyCommand=x", "  --upload-pack=x",
        "ext::sh -c id", "file::/etc/passwd", "fd::3", "persistent-https::x",
        "git://host/r.git", "ftp://host/r.git", "foo://x",
        "file://localhost/srv/r.git", "file://srv/r.git", "file:///srv/%2e%2e/r.git",
        "-host:path", "user@-host:path", "@host:path", "host:", " https://x/r.git",
        # R10-9: a drive-relative path hangs off that drive's cwd; refused.
        "c:repo", "C:", "x:../../other",
    ])
    def test_invalid(self, url):
        from app.core.git_service import classify_clone_url
        kind, reason = classify_clone_url(url)
        assert kind == "invalid" and reason


class TestStripUrlUserinfo:
    @pytest.mark.parametrize("url, expected", [
        ("https://ghp_tok@github.com/o/r.git", "https://github.com/o/r.git"),
        ("https://alice:s3cr3t@git.example.com/r.git", "https://git.example.com/r.git"),
        ("http://x-access-token:t@host:8080/r", "http://host:8080/r"),
        ("https://github.com/o/r.git", "https://github.com/o/r.git"),
        ("git@github.com:o/r.git", "git@github.com:o/r.git"),
        ("ssh://git@github.com/o/r.git", "ssh://git@github.com/o/r.git"),
        ("git+ssh://git@github.com/o/r.git", "git+ssh://git@github.com/o/r.git"),
        ("/srv/repos/r.git", "/srv/repos/r.git"),
        (None, None),
    ])
    def test_strip(self, url, expected):
        from app.core.scrub import strip_url_userinfo
        assert strip_url_userinfo(url) == expected


class TestStatusRemoteUrl:
    """get_status never carries a clone token, on either reader."""

    def test_native_status_is_stripped_and_not_mutated(self, git):
        native = MagicMock()
        raw = {"is_git_repo": True, "remote_url": "https://ghp_tok@github.com/o/r.git"}
        native.get_status.return_value = raw
        with patch("app.core.git_service._native_reader", native), \
             patch("app.core.git_service._git_cache") as cache:
            cache.get.return_value = None
            status = git.get_status("/tmp/work-native")
        assert status["remote_url"] == "https://github.com/o/r.git"
        assert raw["remote_url"] == "https://ghp_tok@github.com/o/r.git"

    def test_cli_status_is_stripped(self, git, tmp_path):
        import subprocess
        repo = tmp_path / "r"
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "remote", "add", "origin",
                        "https://alice:s3cr3t@git.example.com/r.git"], check=True)
        with patch("app.core.git_service._native_reader", None):
            git.invalidate_status_cache(str(repo))
            status = git.get_status(str(repo))
        assert status["is_git_repo"] is True
        assert status["remote_url"] == "https://git.example.com/r.git"
