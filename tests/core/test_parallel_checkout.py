"""`git worktree add` uses Git's parallel checkout on Windows, through real Git."""
import subprocess

import pytest

from app.core import git_worktree_ops
from app.core.git_service import GitService

FILES = 150  # above Git's checkout.thresholdForParallelism (100)


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True,
                          timeout=30).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@example.test")
    git(repo, "config", "user.name", "Test")
    for index in range(FILES):
        (repo / f"file-{index}.txt").write_text(f"{index}\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "Files")
    return repo


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(git_worktree_ops, "_IS_WINDOWS", True)
    monkeypatch.setattr(git_worktree_ops.os, "cpu_count", lambda: 16)


def _traced_add(monkeypatch, repo, dest):
    """Add a worktree; return ``(success, env of the add, every args list run)``."""
    service = GitService()
    run, seen, calls = service._run_git, [], []

    def traced(cwd, args, **kwargs):
        calls.append(list(args))
        if args[:2] == ["worktree", "add"]:
            seen.append(kwargs.get("env"))
        return run(cwd, args, **kwargs)

    monkeypatch.setattr(service, "_run_git", traced)
    success, error = service.add_worktree(str(repo), str(dest), "topic", new_branch=True)
    assert success, error
    assert len(seen) == 1
    return seen[0], calls


def _config(env):
    return {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"]
            for i in range(int(env["GIT_CONFIG_COUNT"]))}


def test_windows_add_checks_out_in_parallel_and_completely(repo, tmp_path, windows, monkeypatch):
    dest = tmp_path / "worktree"

    env, _ = _traced_add(monkeypatch, repo, dest)

    assert _config(env) == {"checkout.workers": str(git_worktree_ops.PARALLEL_CHECKOUT_WORKERS)}
    # Git reads the setting from that environment, and for that call only.
    assert subprocess.run(["git", "config", "--get", "checkout.workers"], cwd=repo, env=env,
                          capture_output=True, text=True, timeout=10).stdout.strip() == "8"
    assert subprocess.run(["git", "config", "--get", "checkout.workers"], cwd=repo,
                          capture_output=True, timeout=10).returncode == 1
    assert sorted(p.name for p in dest.glob("file-*.txt")) == sorted(f"file-{i}.txt" for i in range(FILES))
    assert git(dest, "status", "--porcelain") == ""
    assert git(dest, "symbolic-ref", "--short", "HEAD") == "topic"


def test_small_machine_keeps_the_sequential_checkout(repo, tmp_path, windows, monkeypatch):
    monkeypatch.setattr(git_worktree_ops.os, "cpu_count", lambda: 1)

    env, calls = _traced_add(monkeypatch, repo, tmp_path / "worktree")

    assert env is None
    assert ["config", "--get", "checkout.workers"] not in calls


def test_users_own_setting_is_left_alone(repo, tmp_path, windows, monkeypatch):
    git(repo, "config", "checkout.workers", "1")

    env, _ = _traced_add(monkeypatch, repo, tmp_path / "worktree")

    assert env is None


def test_other_platforms_run_git_as_before(repo, tmp_path, monkeypatch):
    monkeypatch.setattr(git_worktree_ops, "_IS_WINDOWS", False)

    env, calls = _traced_add(monkeypatch, repo, tmp_path / "worktree")

    assert env is None
    assert ["config", "--get", "checkout.workers"] not in calls


def test_existing_environment_config_is_appended_to(repo, windows, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.autocrlf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "false")

    env = git_worktree_ops.parallel_checkout_env(GitService()._run_git, str(repo))

    assert _config(env) == {"core.autocrlf": "false", "checkout.workers": "8"}
