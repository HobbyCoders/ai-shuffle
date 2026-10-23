"""Tree-contained Git deadlines and checkout-wide single-flight regressions (#436)."""

import os
import subprocess
import sys
import threading
import time
from concurrent.futures import Future
from unittest.mock import Mock

import psutil
import pytest

from app.core import git_service as gs
from app.core.git_cache import _git_cache, checkout_key
from tests.git_repo_helpers import hermetic_git_env, init_repo, write


@pytest.fixture
def svc(monkeypatch):
    monkeypatch.setattr(gs, "_status_reads", gs._StatusReads())
    _git_cache.invalidate_prefix("")
    yield gs.GitService()
    _git_cache.invalidate_prefix("")


def _ok(label):
    return {"is_git_repo": True, "current_branch": label}


def _track_waiters(monkeypatch, count):
    joined = threading.Event()
    lock = threading.Lock()
    waits = []

    class TrackingFuture(Future):
        def result(self, timeout=None):
            assert timeout is not None
            with lock:
                waits.append(timeout)
                if len(waits) >= count:
                    joined.set()
            return super().result(timeout=timeout)

    monkeypatch.setattr(gs, "Future", TrackingFuture)
    return joined


def _reader(svc, path, results):
    thread = threading.Thread(target=lambda: results.append(svc.get_status(path)), daemon=True)
    thread.start()
    return thread


@pytest.mark.skipif(os.name == "nt", reason="real POSIX process-group containment")
def test_hung_git_tree_is_killed_and_all_status_waiters_are_released(svc, monkeypatch, tmp_path):
    # The redirector, child and grandchild inherit output handles. With the old
    # PIPE runner an orphan kept them open after only the redirector was killed.
    fake = tmp_path / "git"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import os, subprocess, sys, time\n"
        "from pathlib import Path\n"
        "with open('pids', 'a') as f: f.write(str(os.getpid()) + '\\n')\n"
        "level = int(sys.argv[1]) if len(sys.argv) > 1 else 0\n"
        "if level < 2:\n"
        "    child = subprocess.Popen([sys.executable, __file__, str(level + 1)])\n"
        "else:\n"
        "    Path('ready').touch()\n"
        "while True: time.sleep(60)\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    joined = _track_waiters(monkeypatch, 7)
    started = threading.Event()
    release = threading.Event()
    calls = []

    def read(path):
        calls.append(path)
        started.set()
        assert release.wait(5)
        svc._run_git(path, [], timeout=1)
        pytest.fail("hung git returned successfully")

    monkeypatch.setattr(svc, "_get_status_cli", read)
    results = []
    path = str(tmp_path)
    threads = [_reader(svc, path, results)]
    assert started.wait(5)
    threads.extend(_reader(svc, path, results) for _ in range(7))
    try:
        assert joined.wait(5)
        began = time.monotonic()
        release.set()
        for thread in threads:
            thread.join(5)
        assert not any(thread.is_alive() for thread in threads)
        assert time.monotonic() - began < 1 + 3
        assert calls == [path]
        assert len(results) == 8 and all(r is results[0] for r in results)
        assert not results[0]["is_git_repo"]
        assert "timed out" in svc.get_status_error(path)
        assert (tmp_path / "ready").exists(), "grandchild must have started"
        pids = [int(pid) for pid in (tmp_path / "pids").read_text().splitlines()]
        assert len(pids) == 3
        # Orphans can remain zombies until the container's init reaps them.
        def alive(pid):
            try:
                return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
            except psutil.NoSuchProcess:
                return False

        deadline = time.monotonic() + 1
        while any(alive(pid) for pid in pids) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not any(alive(pid) for pid in pids)
    finally:
        release.set()
        if (tmp_path / "pids").exists():
            for pid in (tmp_path / "pids").read_text().splitlines():
                try:
                    psutil.Process(int(pid)).kill()
                except psutil.NoSuchProcess:
                    pass
        for thread in threads:
            thread.join(5)


@pytest.mark.parametrize("kill_fails", [False, True])
def test_windows_tree_kill_and_reap_are_bounded(svc, monkeypatch, tmp_path, kill_fails):
    monkeypatch.setattr(gs, "_IS_WINDOWS", True)
    monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False)
    proc = Mock(pid=1234)
    expired = subprocess.TimeoutExpired(["git", "status"], 0.1)
    proc.wait.side_effect = [expired, subprocess.TimeoutExpired("git", 1)]
    popen = Mock(return_value=proc)
    kill = Mock(return_value=subprocess.CompletedProcess([], 0))
    if kill_fails:
        kill.side_effect = subprocess.TimeoutExpired("taskkill", 2)
    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(subprocess, "run", kill)

    with pytest.raises(subprocess.TimeoutExpired) as caught:
        svc._run_git(str(tmp_path), ["status"], timeout=0.1)

    assert caught.value is expired
    kwargs = popen.call_args.kwargs
    assert kwargs["creationflags"] == 0x200
    assert "start_new_session" not in kwargs
    assert kwargs["stdout"].closed and kwargs["stderr"].closed
    assert kwargs["stdout"] is not subprocess.PIPE
    assert kill.call_args.args[0] == ["taskkill", "/T", "/F", "/PID", "1234"]
    assert kill.call_args.kwargs["timeout"] == 2
    assert kill.call_args.kwargs["stdout"] == subprocess.DEVNULL
    assert kill.call_args.kwargs["stderr"] == subprocess.DEVNULL
    assert [call.kwargs["timeout"] for call in proc.wait.call_args_list] == [0.1, 1]
    proc.kill.assert_called_once()
    proc.communicate.assert_not_called()  # no post-kill pipe drain at all


def test_runner_preserves_completed_process_and_check_contract(svc, monkeypatch, tmp_path):
    proc = Mock(returncode=3)

    def spawn(command, **kwargs):
        kwargs["stdout"].write(b"out\xff\r\n")
        kwargs["stderr"].write(b"error\n")
        return proc

    monkeypatch.setattr(subprocess, "Popen", spawn)
    result = svc._run_git(str(tmp_path), ["status"])
    assert (result.args, result.returncode, result.stdout, result.stderr) == (
        ["git", "status"], 3, "out\ufffd\n", "error\n",
    )
    with pytest.raises(subprocess.CalledProcessError) as caught:
        svc._run_git(str(tmp_path), ["status"], check=True)
    assert caught.value.output == result.stdout
    assert caught.value.stderr == result.stderr


@pytest.mark.parametrize("stale", [False, True])
def test_waiter_deadline_does_not_replace_an_active_reader(svc, monkeypatch, tmp_path, stale):
    path = str(tmp_path)
    if stale:
        _git_cache.put(f"status:{checkout_key(path)}", (0, _ok("last-good")))
        svc.invalidate_status_cache(path)
    started, release = threading.Event(), threading.Event()
    calls = []

    def read(_path):
        calls.append(_path)
        started.set()
        assert release.wait(5)
        return _ok("new")

    monkeypatch.setattr(svc, "_get_status_cli", read)
    monkeypatch.setattr(gs, "STATUS_WAIT_TIMEOUT_SECONDS", 0.05)
    early = []
    thread = _reader(svc, path, early)
    try:
        assert started.wait(5)
        svc.invalidate_status_cache(path)
        began = time.monotonic()
        late = svc.get_status(path)
        assert time.monotonic() - began < 1
        assert calls == [path]
        if stale:
            assert late["current_branch"] == "last-good"
        else:
            assert not late["is_git_repo"]
            assert "timed out waiting" in svc.get_status_error(path)
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert svc.get_status(path)["current_branch"] == "new"


def test_invalidations_coalesce_one_nonoverlapping_followup(svc, monkeypatch, tmp_path):
    path = str(tmp_path)
    first_started, release_first = threading.Event(), threading.Event()
    second_started, release_second = threading.Event(), threading.Event()
    joined = _track_waiters(monkeypatch, 8)
    lock = threading.Lock()
    state = {"active": 0, "max_active": 0, "calls": 0}

    def read(_path):
        with lock:
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
            state["calls"] += 1
            number = state["calls"]
        try:
            if number == 1:
                first_started.set()
                assert release_first.wait(5)
                return _ok("before")
            second_started.set()
            assert release_second.wait(5)
            return _ok("after")
        finally:
            with lock:
                state["active"] -= 1

    monkeypatch.setattr(svc, "_get_status_cli", read)
    early, late = [], []
    threads = [_reader(svc, path, early)]
    try:
        assert first_started.wait(5)
        for _ in range(8):
            svc.invalidate_status_cache(path)
            threads.append(_reader(svc, path, late))
        assert joined.wait(5)
        assert not second_started.is_set()
        assert state["calls"] == 1
        release_first.set()
        assert second_started.wait(5)
        release_second.set()
        for thread in threads:
            thread.join(5)
        assert not any(thread.is_alive() for thread in threads)
        assert state == {"active": 0, "max_active": 1, "calls": 2}
        assert early[0]["current_branch"] == "before"
        assert len(late) == 8 and all(r["current_branch"] == "after" for r in late)
        assert svc.get_status(path) is late[0]
    finally:
        release_first.set()
        release_second.set()
        for thread in threads:
            thread.join(5)


def test_followup_reads_real_git_after_a_tracked_edit(svc, monkeypatch, tmp_path):
    hermetic_git_env(monkeypatch, tmp_path)
    repo = init_repo(tmp_path / "repo", {"tracked.txt": "before\n"})
    path = str(repo)
    real_read = svc._get_status_cli
    first_started, release = threading.Event(), threading.Event()
    joined = _track_waiters(monkeypatch, 1)
    calls = []

    def read(directory):
        calls.append(directory)
        status = real_read(directory)
        if len(calls) == 1:
            first_started.set()
            assert release.wait(5)
        return status

    monkeypatch.setattr(svc, "_get_status_cli", read)
    early, late = [], []
    threads = [_reader(svc, path, early)]
    try:
        assert first_started.wait(5)
        write(repo / "tracked.txt", "after\n")
        svc.invalidate_status_cache(path)
        threads.append(_reader(svc, path, late))
        assert joined.wait(5)
        assert calls == [path]
        release.set()
        for thread in threads:
            thread.join(5)
        assert not any(thread.is_alive() for thread in threads)
        assert early[0]["is_clean"] is True
        assert late[0]["is_clean"] is False
        assert late[0]["modified"] == ["tracked.txt"]
        assert svc.get_status(path) is late[0]
        assert calls == [path, path]
    finally:
        release.set()
        for thread in threads:
            thread.join(5)
