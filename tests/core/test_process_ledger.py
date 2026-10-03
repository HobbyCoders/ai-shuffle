"""The on-disk ledger that lets a restart stop what a dead server left running.

Every process here is one the test starts itself and is cleaned up through
its own handle. The "previous run" is simulated by rewriting an entry's
recording server to a process that has already exited, which is exactly what
an abrupt server death leaves behind.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import psutil
import pytest

from app.core import process_ledger

# The base interpreter, not a venv's python.exe: on Windows that is a launcher
# that runs the real interpreter as one more child process.
_PYTHON = getattr(sys, "_base_executable", None) or sys.executable
_SLEEP = "import time; time.sleep(120)"
# Parent that starts a sleeping child, prints its pid, then sleeps too.
_PARENT = (
    "import subprocess, sys, time; "
    f"c = subprocess.Popen([sys.executable, '-c', {_SLEEP!r}]); "
    "print(c.pid, flush=True); time.sleep(120)"
)


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    path = tmp_path / "process-ledger-test.json"
    monkeypatch.setattr(process_ledger, "ledger_path", lambda: path)
    # A real restarted server never parents the previous run's processes; the
    # test process does, so only the test process itself stays protected.
    monkeypatch.setattr(process_ledger, "_protected_pids", lambda ps: {os.getpid()})
    started = []

    def spawn(code=_SLEEP, **kwargs):
        proc = subprocess.Popen([_PYTHON, "-c", code], **kwargs)
        started.append(proc.pid)
        return proc

    yield path, spawn
    for pid in started:  # only what this test started, by pid
        try:
            proc = psutil.Process(pid)
            for child in proc.children(recursive=True):
                child.kill()
            proc.kill()
        except psutil.Error:
            pass


def _dead_server() -> tuple[int, float]:
    """The (pid, start time) of a server process that no longer exists."""
    proc = subprocess.Popen([_PYTHON, "-c", "pass"])
    created = psutil.Process(proc.pid).create_time()
    proc.wait(timeout=30)
    return proc.pid, created


def _as_previous_run(path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    pid, created = _dead_server()
    for entry in data["processes"].values():
        entry["server_pid"], entry["server_create_time"] = pid, created
    path.write_text(json.dumps(data), encoding="utf-8")


def _gone(pid, timeout=10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                return True
        except psutil.NoSuchProcess:
            return True
        time.sleep(0.1)
    return False


def test_a_dead_runs_process_and_its_children_are_stopped(ledger):
    path, spawn = ledger
    root = spawn(_PARENT, stdout=subprocess.PIPE, text=True)
    child_pid = int(root.stdout.readline())
    assert process_ledger.record_process(root.pid, "agent_cli", session_id="s1")
    assert not process_ledger.record_process(root.pid, "agent_cli")  # idempotent
    _as_previous_run(path)

    stopped = process_ledger.reap_previous_run(timeout=5)

    assert set(stopped) == {root.pid, child_pid}
    assert _gone(root.pid) and _gone(child_pid)
    assert json.loads(path.read_text(encoding="utf-8"))["processes"] == {}


def test_a_child_orphaned_before_the_restart_is_found_through_the_snapshot(ledger):
    path, spawn = ledger
    root = spawn(_PARENT, stdout=subprocess.PIPE, text=True)
    child_pid = int(root.stdout.readline())
    process_ledger.record_process(root.pid, "monitor")
    process_ledger.refresh()  # the resource monitor's tick
    root.kill()
    root.wait(timeout=30)
    assert psutil.pid_exists(child_pid)
    _as_previous_run(path)

    stopped = process_ledger.reap_previous_run(timeout=5)

    assert stopped == [child_pid]
    assert _gone(child_pid)


def test_a_live_servers_processes_are_left_alone(ledger):
    path, spawn = ledger
    proc = spawn()
    process_ledger.record_process(proc.pid, "monitor")  # recorded by THIS process

    assert process_ledger.reap_previous_run(timeout=1) == []
    assert proc.poll() is None
    assert str(proc.pid) in json.loads(path.read_text(encoding="utf-8"))["processes"]


@pytest.mark.parametrize("mismatch", ["start_time", "cmdline"])
def test_a_recycled_or_different_process_is_never_touched(ledger, mismatch):
    path, spawn = ledger
    proc = spawn()
    process_ledger.record_process(proc.pid, "agent_cli")
    _as_previous_run(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    entry = data["processes"][str(proc.pid)]
    if mismatch == "start_time":
        entry["create_time"] -= 3600  # same pid, a different (older) process
    else:
        entry["cmdline"] = "claude --some-other-session"
    path.write_text(json.dumps(data), encoding="utf-8")

    assert process_ledger.reap_previous_run(timeout=1) == []
    assert proc.poll() is None


def test_refresh_drops_exited_entries(ledger):
    path, spawn = ledger
    proc = spawn("pass")
    process_ledger.record_process(proc.pid, "monitor")
    proc.wait(timeout=30)

    process_ledger.refresh()

    assert json.loads(path.read_text(encoding="utf-8"))["processes"] == {}


def test_bad_input_and_a_missing_ledger_never_raise(ledger):
    assert process_ledger.record_process(None, "monitor") is False
    assert process_ledger.record_process(-1, "monitor") is False
    assert process_ledger.reap_previous_run() == []
    assert process_ledger.refresh() == 0
    process_ledger.forget_process(None)
