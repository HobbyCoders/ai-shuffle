"""Startup repair uses real config/worktree state; only daemon stops are faked."""
import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core import fsmonitor_cleanup as cleanup
from app.db import checkouts, database, projects_v2
from tests.git_repo_helpers import git, hermetic_git_env, init_repo
from tests.shared_fixtures import foreign_keys_off  # noqa: F401


@pytest.fixture
def install(mock_db, monkeypatch, tmp_path):
    hermetic_git_env(monkeypatch, tmp_path)
    monkeypatch.setattr(cleanup, "sys", SimpleNamespace(platform="darwin"))
    return f"{cleanup.MARKER}:{cleanup.settings.effective_instance_id}"


def register(repo, name="repair"):
    database.create_project(name, name, None, str(repo))


@pytest.fixture
def stops(monkeypatch):
    calls = []
    real_git = cleanup._git

    def run(path, *args, **kwargs):
        if args == ("fsmonitor--daemon", "stop"):
            # The real config mutation must precede each daemon stop.
            assert git(path, "config", "--local", "--get", "core.fsmonitor", check=False) == ""
            calls.append(Path(path))
            return 0, ""
        return real_git(path, *args, **kwargs)

    monkeypatch.setattr(cleanup, "_git", run)
    return calls


@pytest.mark.parametrize("platform", ["win32", "darwin"])
def test_registered_projects_and_worktrees_are_repaired_once(install, stops, tmp_path, monkeypatch, caplog, platform):
    monkeypatch.setattr(cleanup, "sys", SimpleNamespace(platform=platform))
    repo = init_repo(tmp_path / "repo")
    register(repo)
    worktree = tmp_path / "worktree with spaces-é"
    git(repo, "worktree", "add", "-q", "-b", "repair", str(worktree))
    git(repo, "config", "--local", "core.fsmonitor", "true")
    with caplog.at_level(logging.INFO, logger=cleanup.__name__):
        cleanup._cleanup(threading.Event())
    assert set(stops) == {repo, worktree}
    assert len(stops) == 2
    assert git(repo, "config", "--local", "--get", "core.fsmonitor", check=False) == ""
    assert database.get_system_setting(install) == "1"
    lines = [r.message for r in caplog.records if r.name == cleanup.__name__]
    assert len(lines) == 1
    assert "repos fixed=1, daemons stopped=2" in lines[0]
    # User explicitly re-enables it after migration: a later boot must respect it.
    git(repo, "config", "--local", "core.fsmonitor", "true")
    cleanup._cleanup(threading.Event())
    assert git(repo, "-c", "core.fsmonitor=false", "config", "--local", "--get", "core.fsmonitor") == "true"
    assert len(stops) == 2


@pytest.mark.parametrize("value", [None, "false", "TRUE", "1", "yes", "custom-hook", " true "])
def test_only_literal_local_true_is_changed(install, stops, tmp_path, value):
    repo = init_repo(tmp_path / "repo")
    register(repo)
    # A real temporary global config, never the test machine's config.
    git(repo, "config", "--global", "core.fsmonitor", "true")
    if value is not None:
        git(repo, "config", "--local", "core.fsmonitor", value)
    before = (repo / ".git" / "config").read_bytes()
    global_before = Path(os.environ["GIT_CONFIG_GLOBAL"]).read_bytes()
    cleanup._cleanup(threading.Event())
    assert (repo / ".git" / "config").read_bytes() == before
    assert Path(os.environ["GIT_CONFIG_GLOBAL"]).read_bytes() == global_before
    assert not stops
    assert database.get_system_setting(install) == "1"


def test_multiple_local_values_are_left_alone(install, stops, tmp_path):
    repo = init_repo(tmp_path / "repo")
    register(repo)
    git(repo, "config", "--add", "core.fsmonitor", "true")
    git(repo, "config", "--add", "core.fsmonitor", "custom-hook")
    before = (repo / ".git" / "config").read_bytes()
    cleanup._cleanup(threading.Event())
    assert (repo / ".git" / "config").read_bytes() == before
    assert not stops


def test_local_true_removed_without_touching_global(install, stops, tmp_path):
    repo = init_repo(tmp_path / "repo")
    register(repo)
    git(repo, "config", "--global", "core.fsmonitor", "true")
    git(repo, "config", "--local", "core.fsmonitor", "true")
    before = Path(os.environ["GIT_CONFIG_GLOBAL"]).read_bytes()
    cleanup._cleanup(threading.Event())
    assert Path(os.environ["GIT_CONFIG_GLOBAL"]).read_bytes() == before
    assert git(repo, "-c", "core.fsmonitor=false", "config", "--local", "--get", "core.fsmonitor", check=False) == ""
    assert stops == [repo]


def test_linux_noop(install, monkeypatch):
    monkeypatch.setattr(cleanup, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(cleanup, "_project_repos", lambda: pytest.fail("enumerated on Linux"))
    monkeypatch.setattr(database, "get_system_setting", lambda key: pytest.fail("read marker on Linux"))
    cleanup._cleanup(threading.Event())


def test_canonical_local_checkouts_not_remote_ones(install, stops, tmp_path):
    repo = init_repo(tmp_path / "repo")
    remote = init_repo(tmp_path / "remote")
    project = projects_v2.upsert_project("local:cleanup", name="Cleanup")
    checkouts.upsert_checkout(project["id"], cleanup.settings.effective_instance_id,
                             str(repo), branch="main")
    checkouts.upsert_checkout(project["id"], "another-install", str(remote), branch="main")
    for path in (repo, remote):
        git(path, "config", "--local", "core.fsmonitor", "true")
    cleanup._cleanup(threading.Event())
    assert stops == [repo]
    assert git(remote, "-c", "core.fsmonitor=false", "config", "--local", "--get", "core.fsmonitor") == "true"


@pytest.mark.parametrize("failure", ["error", "timeout", "not-running"])
def test_failed_stops_are_retried_after_unset(install, tmp_path, monkeypatch, failure):
    repo = init_repo(tmp_path / "repo")
    register(repo)
    git(repo, "config", "--local", "core.fsmonitor", "true")
    real_git = cleanup._git
    calls = []

    def run(path, *args, **kwargs):
        if args == ("fsmonitor--daemon", "stop"):
            calls.append(path)
            if failure == "timeout":
                raise subprocess.TimeoutExpired("git", 5)
            return (1, "fatal: daemon not running" if failure == "not-running" else "failed")
        return real_git(path, *args, **kwargs)

    monkeypatch.setattr(cleanup, "_git", run)
    cleanup._cleanup(threading.Event())
    state = database.get_system_setting(install)
    if failure == "not-running":
        assert state == "1"
    else:
        assert json.loads(state) == [os.path.normcase(os.path.abspath(repo))]
        assert git(repo, "config", "--local", "--get", "core.fsmonitor", check=False) == ""
        monkeypatch.setattr(cleanup, "_git", lambda *a, **kw: (0, ""))
        cleanup._cleanup(threading.Event())
        assert database.get_system_setting(install) == "1"
    assert len(calls) == 1


def test_registry_worktree_survives_missing_root(install, stops, tmp_path, monkeypatch):
    repo = init_repo(tmp_path / "repo")
    git(repo, "config", "--local", "core.fsmonitor", "true")
    monkeypatch.setattr(cleanup, "_project_repos", lambda: [
        {"path": str(tmp_path / "missing"), "registered": [{"path": str(repo)}]}
    ])
    cleanup._cleanup(threading.Event())
    assert stops == [repo]


@pytest.mark.parametrize("where", ["enumerate", "read-marker", "write-marker", "git"])
def test_failures_never_escape(install, tmp_path, monkeypatch, where):
    repo = init_repo(tmp_path / "repo")
    register(repo)
    git(repo, "config", "--local", "core.fsmonitor", "true")

    def fail(*args, **kwargs):
        raise OSError("unavailable")

    target, name = {
        "enumerate": (cleanup, "_project_repos"),
        "read-marker": (database, "get_system_setting"),
        "write-marker": (database, "set_system_setting"),
        "git": (cleanup, "_git"),
    }[where]
    monkeypatch.setattr(target, name, fail)
    cleanup._cleanup(threading.Event())


def test_total_budget_and_cancellation_keep_marker_unfinished(install, stops, tmp_path, monkeypatch):
    repo = init_repo(tmp_path / "repo")
    register(repo)
    git(repo, "config", "--local", "core.fsmonitor", "true")
    monkeypatch.setattr(cleanup, "TOTAL_TIMEOUT", 0)
    cleanup._cleanup(threading.Event())
    assert database.get_system_setting(install) is None
    assert not stops
    monkeypatch.setattr(cleanup, "TOTAL_TIMEOUT", 60)
    cancelled = threading.Event()
    cancelled.set()
    cleanup._cleanup(cancelled)
    assert database.get_system_setting(install) is None


def test_git_timeout_is_bounded_without_pipes(monkeypatch):
    def run(command, **kwargs):
        assert kwargs["stdout"] != subprocess.PIPE
        assert kwargs["stderr"] == subprocess.STDOUT
        assert 0 < kwargs["timeout"] <= cleanup.COMMAND_TIMEOUT
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(subprocess.TimeoutExpired):
        cleanup._git("unused", "config", deadline=time.monotonic() + 2, cancelled=threading.Event())


@pytest.mark.asyncio
async def test_background_timeout_does_not_raise_and_signals_cancellation(monkeypatch):
    events = []

    async def timed_out(function, event):
        events.append(event)
        raise TimeoutError("slow registry")

    monkeypatch.setattr(cleanup.asyncio, "to_thread", timed_out)
    await cleanup.cleanup_once()
    assert len(events) == 1 and events[0].is_set()


@pytest.mark.asyncio
async def test_background_task_runs_off_event_loop(monkeypatch):
    loop_thread = threading.get_ident()
    threads = []
    monkeypatch.setattr(cleanup, "_cleanup", lambda cancelled: threads.append(threading.get_ident()))
    await cleanup.cleanup_once()
    assert len(threads) == 1 and threads[0] != loop_thread
