"""Git status reads stay out of the interpreter and scale with active checkouts (#436).

pygit2 held the GIL for a whole native ``status()``: one cold read per worktree
froze every thread in the app for minutes on a busy disk. Status is now a
``git`` subprocess the calling thread only waits on, with at most one read per
checkout in flight, a short cache invalidated by changes, and a stale or
"unknown" answer instead of an exception when git is slow or the checkout is
broken.
"""

import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from app.core.git_cache import _git_cache
from app.core.git_service import GitService, _quiet_git_env
from tests.git_repo_helpers import commit_file, git, hermetic_git_env, init_repo, write

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def svc(monkeypatch, tmp_path):
    hermetic_git_env(monkeypatch, tmp_path)
    _git_cache.invalidate_prefix("")
    return GitService()


@pytest.fixture
def repo(tmp_path, svc):
    return init_repo(tmp_path / "repo", {"a.txt": "a\n"})


def _ok(**extra):
    return {"is_git_repo": True, "is_clean": True, "staged": [], "modified": [],
            "untracked": [], "conflicts": [], **extra}


def test_no_git_read_path_loads_pygit2():
    """The import graph of every git read path stays free of libgit2."""
    code = (
        "import sys\n"
        "import app.api.git, app.api.git_worktrees, app.api.git_owner\n"
        "import app.core.git_service, app.core.worktree_manager, app.core.git_activity\n"
        "assert 'pygit2' not in sys.modules, 'pygit2 imported'\n"
    )
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(REPO_ROOT), str(REPO_ROOT / "fluid")])}
    result = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, env=env,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr[-2000:]


def test_concurrent_reads_of_one_checkout_share_one_git_call(svc, monkeypatch, tmp_path):
    started, release = threading.Event(), threading.Event()
    calls = []

    def slow_read(path):
        calls.append(path)
        started.set()
        release.wait(5)
        return _ok(current_branch="main")

    monkeypatch.setattr(svc, "_get_status_cli", slow_read)
    path = str(tmp_path / "wt")
    results = []
    threads = [threading.Thread(target=lambda: results.append(svc.get_status(path)))
               for _ in range(8)]
    threads[0].start()
    assert started.wait(5)
    for thread in threads[1:]:
        thread.start()
    release.set()
    for thread in threads:
        thread.join(5)

    assert calls == [path]
    assert len(results) == 8 and all(r is results[0] for r in results)


def test_a_read_started_before_an_invalidation_is_not_reused_or_cached(svc, monkeypatch, tmp_path):
    """A status asked for after a change never describes the checkout before it."""
    path = str(tmp_path / "wt")
    first_started, release_first = threading.Event(), threading.Event()
    answers = iter(["before", "after"])

    def read(_path):
        label = next(answers)
        if label == "before":
            first_started.set()
            release_first.wait(5)
        return _ok(current_branch=label)

    monkeypatch.setattr(svc, "_get_status_cli", read)
    early = []
    reader = threading.Thread(target=lambda: early.append(svc.get_status(path)))
    reader.start()
    assert first_started.wait(5)

    svc.invalidate_status_cache(path)  # e.g. a turn just edited files
    late = svc.get_status(path)        # must not wait for (or take) the old read
    release_first.set()
    reader.join(5)

    assert early[0]["current_branch"] == "before"
    assert late["current_branch"] == "after"
    assert svc.get_status(path)["current_branch"] == "after"  # the old read was not cached


def test_invalidation_bumps_the_generation_for_any_spelling_of_the_path(svc, tmp_path):
    path = tmp_path / "wt"
    before = svc.status_generation(str(path))
    svc.invalidate_status_cache(str(path) + os.sep)
    assert svc.status_generation(os.path.join(str(tmp_path), ".", "wt")) == before + 1


def test_timeout_serves_the_last_good_status_and_never_raises(svc, repo, monkeypatch, caplog):
    good = svc.get_status(str(repo))
    assert good["is_git_repo"] is True

    real_run = svc._run_git

    def hanging_status(cwd, args, timeout=30, **kwargs):
        if args and args[0] == "status":
            raise subprocess.TimeoutExpired(["git", *args], timeout)
        return real_run(cwd, args, timeout=timeout, **kwargs)

    monkeypatch.setattr(svc, "_run_git", hanging_status)
    # A turn just ended there: the cached status is no longer fresh, but it is
    # still the best answer while git does not respond.
    svc.invalidate_status_cache(str(repo))

    stale = svc.get_status(str(repo))

    assert stale == good
    assert svc.get_status_error(str(repo)) is None
    assert any("timed out" in r.getMessage() for r in caplog.records)


def test_timeout_with_nothing_recent_is_unknown_not_an_exception(svc, repo, monkeypatch):
    def hanging(cwd, args, timeout=30, **kwargs):
        raise subprocess.TimeoutExpired(["git", *args], timeout)

    monkeypatch.setattr(svc, "_run_git", hanging)
    svc.invalidate_status_cache(str(repo))

    status = svc.get_status(str(repo))

    assert status["is_git_repo"] is False
    assert "timed out" in svc.get_status_error(str(repo))


def test_missing_git_executable_is_an_error_not_an_exception(svc, repo, monkeypatch):
    def no_git(*_args, **_kwargs):
        raise FileNotFoundError(2, "No such file or directory", "git")

    monkeypatch.setattr(svc, "_run_git", no_git)
    svc.invalidate_status_cache(str(repo))

    assert svc.get_status(str(repo))["is_git_repo"] is False
    assert svc.get_status_error(str(repo))


def test_base_comparison_follows_head_without_invalidation(svc, tmp_path, repo):
    """Ahead counts are cached per HEAD commit: a commit an agent makes from its
    own shell (which the app never hears about) shows on the next read."""
    git(repo, "checkout", "-q", "-b", "feature")
    svc.invalidate_status_cache(str(repo))
    assert svc._get_status_cli(str(repo))["ahead"] == 0
    commit_file(repo, "b.txt", "b\n", "feat: b")
    assert svc._get_status_cli(str(repo))["ahead"] == 1


def test_status_takes_no_optional_index_lock(svc, repo):
    """A background read must never hold index.lock while an agent commits.

    Plain ``git status`` would rewrite the index here to record the touched
    file's new stat data (an opportunistic refresh under index.lock).
    """
    os.utime(repo / "a.txt", (1, 1))  # same content, stat info differs from the index
    index = repo / ".git" / "index"
    before = index.read_bytes()
    svc.invalidate_status_cache(str(repo))

    status = svc.get_status(str(repo))

    assert status["is_clean"] is True
    assert index.read_bytes() == before


def test_untracked_directories_are_collapsed_and_expandable(svc, repo):
    write(repo / "tree" / "one.txt", "1\n")
    write(repo / "tree" / "nested" / "two.txt", "2\n")
    write(repo / "loose.txt", "x\n")
    svc.invalidate_status_cache(str(repo))

    untracked = svc.get_status(str(repo))["untracked"]

    assert sorted(untracked) == ["loose.txt", "tree/"]
    assert sorted(svc.expand_untracked_dirs(str(repo), untracked)) == [
        "loose.txt", "tree/nested/two.txt", "tree/one.txt",
    ]


def test_quiet_env_disables_optional_locks_and_fsmonitor_after_existing_config():
    env = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "url.x.insteadOf",
           "GIT_CONFIG_VALUE_0": "y"}
    _quiet_git_env(env)
    assert env["GIT_OPTIONAL_LOCKS"] == "0"
    assert env["GIT_CONFIG_COUNT"] == "2"
    assert (env["GIT_CONFIG_KEY_0"], env["GIT_CONFIG_VALUE_0"]) == ("url.x.insteadOf", "y")
    assert (env["GIT_CONFIG_KEY_1"], env["GIT_CONFIG_VALUE_1"]) == ("core.fsmonitor", "false")

    explicit = {"GIT_OPTIONAL_LOCKS": "1"}
    _quiet_git_env(explicit)
    assert explicit["GIT_OPTIONAL_LOCKS"] == "1"


def test_current_branch_from_files_matches_git(svc, repo, tmp_path, monkeypatch):
    """The busy-checkout guard reads every active session's branch per poll;
    reading HEAD from disk must agree with git in each layout it handles, and
    leave the rest (unborn, subfolder) to git."""
    calls = []
    real = svc._get_current_branch_cli
    monkeypatch.setattr(svc, "_get_current_branch_cli",
                        lambda path: calls.append(path) or real(path))

    assert svc.get_current_branch(str(repo)) == "main"
    git(repo, "checkout", "-q", "-b", "feat/x")
    git(repo, "pack-refs", "--all")  # the branch now lives only in packed-refs
    assert svc.get_current_branch(str(repo)) == "feat/x"
    linked = tmp_path / "linked"
    git(repo, "worktree", "add", "-q", "-b", "wt-branch", str(linked))
    assert svc.get_current_branch(str(linked)) == "wt-branch"
    git(linked, "checkout", "-q", "--detach")
    assert svc.get_current_branch(str(linked)) is None
    assert calls == []

    (repo / "sub").mkdir()
    assert svc.get_current_branch(str(repo / "sub")) == "feat/x"  # git's own discovery
    unborn = init_repo(tmp_path / "unborn", commit=False)
    assert svc.get_current_branch(str(unborn)) is None
    assert calls == [str(repo / "sub"), str(unborn)]
