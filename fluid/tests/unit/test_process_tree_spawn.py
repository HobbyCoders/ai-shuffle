"""Windows: a command joins its kill-on-close job before its shell can run.

The job used to be assigned after the shell had started, so a child the shell
spawned at once could start outside the job and survive Stop or a timeout,
holding the output pipe. :func:`spawn_tree` now starts the shell suspended,
assigns the job, then resumes it; a process it cannot resume is killed, never
left suspended.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from fluid.tools import process_tree as tree_mod


class _FakeProc:
    pid = 4242

    def __init__(self) -> None:
        self.returncode: int | None = None
        self.killed = False

    def kill(self) -> None:
        self.killed = True
        self.returncode = 1

    async def wait(self) -> int:
        return self.returncode or 0


def _as_windows(monkeypatch, calls: list, *, resume_error: Exception | None = None) -> _FakeProc:
    proc = _FakeProc()

    async def fake_exec(*argv, **kwargs):
        calls.append(("spawn", kwargs.get("creationflags")))
        return proc

    def assign(pid):
        calls.append(("assign", pid))
        return 77

    def resume(pid):
        calls.append(("resume", pid))
        if resume_error is not None:
            raise resume_error

    monkeypatch.setattr(tree_mod, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(tree_mod.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(tree_mod, "assign_kill_on_close_job", assign)
    monkeypatch.setattr(tree_mod, "resume_process", resume)
    monkeypatch.setattr(tree_mod, "close_job", lambda job: calls.append(("close", job)))
    return proc


async def test_the_shell_starts_suspended_and_joins_the_job_before_it_runs(monkeypatch) -> None:
    calls: list = []
    proc = _as_windows(monkeypatch, calls)
    got, job = await tree_mod.spawn_tree("cmd", env={}, creationflags=0x4000)
    assert got is proc and job == 77
    # Priority flags kept, CREATE_SUSPENDED added; job before resume.
    assert calls == [("spawn", 0x4000 | 0x4), ("assign", 4242), ("resume", 4242)]


async def test_a_shell_that_cannot_be_resumed_is_killed_not_left_suspended(monkeypatch) -> None:
    calls: list = []
    proc = _as_windows(monkeypatch, calls, resume_error=OSError("access denied"))
    with pytest.raises(OSError, match="access denied"):
        await tree_mod.spawn_tree("cmd", env={})
    assert proc.killed
    assert ("close", 77) in calls


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Objects")
async def test_a_child_spawned_at_once_dies_with_the_tree() -> None:  # pragma: no cover - win32 only
    """The shell spawns a child as its very first act; killing the tree ends it."""
    script = (
        "import subprocess, sys, time\n"
        "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "print(c.pid, flush=True)\n"
        "time.sleep(60)\n"
    )
    proc, job = await tree_mod.spawn_tree(
        sys.executable, "-c", script, env=dict(os.environ), stdout=asyncio.subprocess.PIPE,
    )
    assert job is not None
    assert proc.stdout is not None
    child = int((await asyncio.wait_for(proc.stdout.readline(), 30)).decode())
    try:
        await tree_mod.kill_tree(proc, job)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and _win_alive(child):
            await asyncio.sleep(0.1)
        assert not _win_alive(child), "the child the shell spawned at once survived"
    finally:
        if _win_alive(child):
            subprocess.run(["taskkill", "/F", "/PID", str(child)], capture_output=True)


def _win_alive(pid: int) -> bool:  # pragma: no cover - win32 only
    out = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
        capture_output=True, text=True,
    ).stdout
    return f'"{pid}"' in out
