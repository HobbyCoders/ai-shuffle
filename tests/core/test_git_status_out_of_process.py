"""Git status reads run outside the interpreter and degrade instead of raising (#436).

pygit2 kept the GIL for whole status scans, so one slow checkout froze every
thread in the app. GitService now reads through ``git`` subprocesses: a read
that overruns is killed and the caller gets the last good status, concurrent
callers for one checkout share a single read, and callers that only need
counts skip recursing into untracked folders.
"""

import subprocess
import threading
import time

import pytest

from app.core import git_cache
from app.core import git_service as gs_mod
from app.core.git_service import GitService
from tests.git_repo_helpers import hermetic_git_env, init_repo, write


@pytest.fixture
def svc():
    return GitService()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    hermetic_git_env(monkeypatch, tmp_path)
    path = init_repo(tmp_path / "repo", {"a.txt": "a\n"})
    yield path
    git_cache._status_errors.pop(str(path), None)


def _fresh(svc, path, **kw):
    svc.invalidate_status_cache(str(path))
    return svc.get_status(str(path), **kw)


def test_app_does_not_read_through_pygit2():
    # Every module that consults the reader sees None, so reads take the CLI path.
    from app.core import git_branch_ops, git_diff_ops, git_service, git_worktree_ops

    for module in (git_cache, git_service, git_branch_ops, git_diff_ops, git_worktree_ops):
        assert module._native_reader is None, module.__name__


def test_status_reads_without_optional_locks(svc, repo, monkeypatch):
    calls = []
    real = GitService._run_git

    def spy(self, working_dir, args, *a, **kw):
        calls.append(args)
        return real(self, working_dir, args, *a, **kw)

    monkeypatch.setattr(GitService, "_run_git", spy)
    _fresh(svc, repo)
    status_calls = [args for args in calls if "status" in args]
    assert status_calls and all(args[0] == "--no-optional-locks" for args in status_calls)
    assert all(any(a.startswith("--untracked-files=") for a in args) for args in status_calls)


def test_untracked_modes(svc, repo):
    write(repo / "new" / "one.txt", "1\n")
    write(repo / "new" / "two.txt", "2\n")
    write(repo / "loose.txt", "x\n")

    every_file = _fresh(svc, repo)  # default: per-file paths for the changed-files panel
    assert sorted(every_file["untracked"]) == ["loose.txt", "new/one.txt", "new/two.txt"]

    folders = _fresh(svc, repo, untracked="normal")
    assert sorted(folders["untracked"]) == ["loose.txt", "new/"]
    assert folders["is_clean"] is False

    with pytest.raises(ValueError):
        svc.get_status(str(repo), untracked="no")


def test_timeout_serves_last_good_status(svc, repo, monkeypatch):
    write(repo / "loose.txt", "x\n")
    good = _fresh(svc, repo)
    assert good["untracked"] == ["loose.txt"]

    def overrun(self, working_dir, args, *a, **kw):
        raise subprocess.TimeoutExpired(["git", *args], 15)

    monkeypatch.setattr(GitService, "_run_git", overrun)
    stale = _fresh(svc, repo)
    assert stale["stale"] is True
    assert stale["untracked"] == ["loose.txt"] and stale["is_git_repo"] is True
    assert git_cache.get_status_error(str(repo)) is None
    # Polls within the TTL reuse the stale answer instead of starting more slow reads.
    assert svc.get_status(str(repo)) is stale


def test_timeout_without_a_good_read_is_unknown_not_an_exception(svc, tmp_path, monkeypatch):
    hermetic_git_env(monkeypatch, tmp_path)
    path = init_repo(tmp_path / "cold", {"a.txt": "a\n"})

    def overrun(self, working_dir, args, *a, **kw):
        raise subprocess.TimeoutExpired(["git", *args], 15)

    monkeypatch.setattr(GitService, "_run_git", overrun)
    try:
        status = _fresh(svc, path)
        assert status["is_git_repo"] is False and "stale" not in status
        assert git_cache.get_status_error(str(path)) == "git status timed out"
    finally:
        git_cache._status_errors.pop(str(path), None)


def test_a_good_read_clears_the_recorded_error(svc, repo, monkeypatch):
    git_cache._status_errors[str(repo)] = "git status timed out"
    assert _fresh(svc, repo)["is_git_repo"] is True
    assert git_cache.get_status_error(str(repo)) is None


def test_concurrent_reads_of_one_checkout_share_one_git_call(svc, repo, monkeypatch):
    svc.invalidate_status_cache(str(repo))
    entered = threading.Event()
    release = threading.Event()
    reads = []

    def slow_read(self, working_dir, untracked="all"):
        reads.append(working_dir)
        entered.set()
        assert release.wait(5)
        result = {"is_git_repo": True, "marker": len(reads)}
        git_cache._git_cache.put(gs_mod._status_cache_key(working_dir, untracked), result)
        return result

    monkeypatch.setattr(GitService, "_get_status_cli", slow_read)
    results = []
    threads = [threading.Thread(target=lambda: results.append(svc.get_status(str(repo))))
               for _ in range(6)]
    threads[0].start()
    assert entered.wait(5)
    for t in threads[1:]:
        t.start()
    # Give the followers time to queue behind the leader, then let it finish.
    time.sleep(0.3)
    release.set()
    for t in threads:
        t.join(5)
    assert reads == [str(repo)]
    assert [r["marker"] for r in results] == [1] * 6
    assert gs_mod._status_reads.in_flight() == 0


def test_single_flight_shares_the_leaders_error():
    flight = gs_mod._SingleFlight()
    gate = threading.Event()
    errors = []

    def boom():
        gate.wait(5)
        raise RuntimeError("read failed")

    def call():
        try:
            flight.run("k", boom)
        except RuntimeError as exc:
            errors.append(str(exc))

    threads = [threading.Thread(target=call) for _ in range(3)]
    for t in threads:
        t.start()
    gate.set()
    for t in threads:
        t.join(5)
    assert errors == ["read failed"] * 3
    assert flight.in_flight() == 0
