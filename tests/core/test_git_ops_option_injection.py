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


@pytest.mark.parametrize("name", ["main", "feat/x", "release-1.2", "user@host", "a+b", "\u00e9t\u00e9"])
def test_plain_branch_names_are_accepted(name):
    assert git_commit_ops._is_plain_branch_name(name)
    assert git_commit_ops._push_refspec(name) == f"refs/heads/{name}:refs/heads/{name}"


@pytest.mark.parametrize("name", [
    "", "@", ":main", "x:main", "+x:main", "+main", "HEAD:refs/tags/v1", "--mirror", "-x",
    "a..b", "a@{1}", "a^", "a~1", "a?", "a*", "a[b", "a\\b", "a b", "a\tb", "a\x7f",
    "/a", "a/", "a//b", "a.", ".a", "a/.b", "a.lock", "a/b.lock/c",
])
def test_refspecs_and_invalid_ref_names_are_not_branch_names(name):
    assert not git_commit_ops._is_plain_branch_name(name)


def test_push_pushes_the_named_branch_to_the_same_remote_branch(repo, tmp_path):
    git(repo, "checkout", "-q", "-b", "feat/x")
    commit_file(repo, "x.txt", "x\n", "feat")

    ok, err = git_service.push(str(repo), branch="feat/x", set_upstream=True)

    assert ok, err
    bare = tmp_path / "origin.git"
    assert git(bare, "rev-parse", "refs/heads/feat/x") == git(repo, "rev-parse", "HEAD")
    assert git(repo, "rev-parse", "--abbrev-ref", "feat/x@{upstream}") == "origin/feat/x"


def _refspec_pushes_leave_remote_refs_alone(repo, tmp_path):
    """R8-4: ``branch`` reached ``git push`` as a refspec: ``:keep-me``
    deleted the remote branch, ``HEAD:refs/tags/v1`` wrote any ref."""
    bare = tmp_path / "origin.git"
    keep = git(bare, "rev-parse", "refs/heads/keep-me")
    for branch in (":keep-me", "HEAD:refs/tags/v1", "+main:keep-me"):
        ok, err = git_service.push(str(repo), branch=branch)
        assert (ok, err) == (False, f"Invalid branch name: {branch!r}")
    assert git(bare, "rev-parse", "refs/heads/keep-me") == keep
    assert git(bare, "tag", "--list") == ""


def test_push_refuses_refspecs_in_the_branch(repo, tmp_path):
    _refspec_pushes_leave_remote_refs_alone(repo, tmp_path)


def test_push_refspec_guard_bites_when_disabled(repo, tmp_path, monkeypatch):
    # The pre-fix argv: the caller's string verbatim in refspec position.
    monkeypatch.setattr(git_commit_ops, "_is_plain_branch_name", lambda branch: True)
    monkeypatch.setattr(git_commit_ops, "_push_refspec", lambda branch: branch)
    with pytest.raises(AssertionError):
        _refspec_pushes_leave_remote_refs_alone(repo, tmp_path)


def test_push_of_a_branch_named_mirror_is_refused_not_a_mirror_push(repo, tmp_path):
    """A branch literally named ``--mirror`` (``git update-ref`` can make one;
    ``git branch`` cannot) is not a branch name git accepts, so it is refused
    rather than pushed; it never reaches git as the ``--mirror`` flag."""
    git(repo, "update-ref", "refs/heads/--mirror", "HEAD")
    git(repo, "symbolic-ref", "HEAD", "refs/heads/--mirror")
    commit_file(repo, "x.txt", "x\n", "on the odd branch")

    ok, err = git_service.push(str(repo), branch="--mirror", set_upstream=True)

    assert (ok, err) == (False, "Invalid branch name: '--mirror'")
    bare = tmp_path / "origin.git"
    # --mirror would have deleted every remote ref the checkout lacks.
    assert git(bare, "branch", "--list", "keep-me") != ""
    assert git(bare, "branch", "--list", "--", "--mirror") == ""


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
