"""Option injection through branch / remote / base names into git argv.

Real repositories under ``tmp_path``. Each case feeds a name that git would
parse as an option if it reached argv verbatim and checks the observable
side effect (a file written, a remote branch deleted) did not happen.
"""

import pytest

from app.core import git_commit_ops
from app.core.git_native import HAS_PYGIT2
from app.core.git_service import git_service
from tests.git_repo_helpers import (
    commit_file,
    git,
    hermetic_git_env,
    init_repo,
    use_backend,
)

BACKENDS = [
    pytest.param("native", marks=pytest.mark.skipif(not HAS_PYGIT2, reason="pygit2 unavailable")),
    "cli",
]


@pytest.fixture
def repo(tmp_path, monkeypatch):
    hermetic_git_env(monkeypatch, tmp_path)
    repo = init_repo(tmp_path / "repo")
    bare = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    git(repo, "remote", "add", "origin", str(bare))
    git(repo, "push", "-q", "-u", "origin", "main")
    git(repo, "push", "-q", "origin", "main:keep-me")
    return repo


def test_push_branch_arg_qualifies_only_option_like_names():
    assert git_commit_ops._push_branch_arg("feat/x") == "feat/x"
    assert git_commit_ops._push_branch_arg("--mirror") == "refs/heads/--mirror"
    assert git_commit_ops._push_branch_arg("-x") == "refs/heads/-x"


def test_push_of_a_branch_named_mirror_is_not_a_mirror_push(repo, tmp_path):
    git(repo, "update-ref", "refs/heads/--mirror", "HEAD")
    git(repo, "symbolic-ref", "HEAD", "refs/heads/--mirror")
    commit_file(repo, "x.txt", "x\n", "on the odd branch")

    ok, err = git_service.push(str(repo), branch="--mirror", set_upstream=True)

    assert ok, err
    bare = tmp_path / "origin.git"
    assert git(bare, "rev-parse", "refs/heads/--mirror") == git(repo, "rev-parse", "HEAD")
    # --mirror would have deleted every remote ref the checkout lacks.
    assert git(bare, "branch", "--list", "keep-me") != ""


@pytest.mark.parametrize("remote", ["--mirror", "-v", "--all"])
def test_push_option_like_remote_is_refused_before_git_runs(repo, monkeypatch, remote):
    def boom(*args, **kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("git ran")

    monkeypatch.setattr(git_service, "_run_git", boom)

    assert git_service.push(str(repo), remote=remote, branch="main") == (
        False, f"Invalid remote name: {remote}",
    )


@pytest.mark.parametrize("backend", BACKENDS)
def test_branch_log_option_like_base_writes_no_file(repo, tmp_path, monkeypatch, backend):
    use_backend(monkeypatch, backend)
    git(repo, "checkout", "-q", "-b", "feat")
    commit_file(repo, "f.txt", "f\n", "feat: one")
    target = tmp_path / "written"

    log, stat = git_service.get_branch_log(str(repo), base_branch=f"--output={target}")

    assert (log, stat) == ("", "")
    assert list(tmp_path.glob("written*")) == []
    # A real base still works on the same backend.
    log, _ = git_service.get_branch_log(str(repo), base_branch="main")
    assert log.endswith("feat: one")


def test_cli_branch_log_guard_is_what_stops_the_write(repo, tmp_path, monkeypatch):
    """Show the CLI path writes the file when the guard is bypassed (the
    unguarded argv is rebuilt here), so the test above is meaningful."""
    use_backend(monkeypatch, "cli")
    target = tmp_path / "written"
    result = git_service._run_git(str(repo), ["log", f"--output={target}..HEAD", "--oneline", "-5"])
    assert result.returncode == 0
    assert [p.name for p in tmp_path.glob("written*")] == ["written..HEAD"]
