"""A command's whole tree dies on timeout, Stop or cancel — also after its shell exited.

native.md Medium #3, tools.md #5, providers.md #6: the hook runner, the Bash
tool and the CLI Monitor all skipped the process-group kill once the shell
(the group leader) had exited, so a descendant still holding the output pipe
ran on, and Bash reported "was killed" for a command still running. A hook
was not killed at all when its turn was cancelled.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

from fluid.tools.registry import ToolCtx

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")


def _alive(pid: int) -> bool:
    """Running (not gone, not a zombie waiting for a reaper)."""
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except FileNotFoundError:
        return False
    except OSError:  # no /proc (macOS): fall back to a signal probe
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True
    return state not in ("Z", "X")


async def _gone(pid: int, within: float = 5.0) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        await asyncio.sleep(0.05)
    return False


async def _wait_for_file(path: Path, within: float = 10.0) -> str:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if path.exists() and path.read_text().strip():
            return path.read_text().strip()
        await asyncio.sleep(0.02)
    raise AssertionError(f"{path} never written")


async def test_kill_tree_ends_the_group_after_its_leader_exited() -> None:
    from fluid.tools.process_tree import kill_tree

    proc = await asyncio.create_subprocess_exec(
        "/bin/sh", "-c", "sleep 60 & echo $!",
        stdout=asyncio.subprocess.PIPE, start_new_session=True,
    )
    assert proc.stdout is not None
    pid = int((await proc.stdout.readline()).decode())
    while proc.returncode is None:  # the shell exits; sleep keeps the pipe
        await asyncio.sleep(0.02)
    assert _alive(pid)
    try:
        await kill_tree(proc)
        assert await _gone(pid)
        # The pipe the leftover held is closed too.
        assert await asyncio.wait_for(proc.stdout.read(), 5) == b""
    finally:
        if _alive(pid):
            os.kill(pid, 9)


async def test_bash_timeout_kills_a_descendant_of_an_exited_shell(tmp_path) -> None:
    from fluid.tools.builtins.bash import handler

    pid_file = tmp_path / "pid"
    out = await handler(
        {"command": f"sleep 60 & echo $! > {pid_file}", "timeout": 1},
        ToolCtx(cwd=str(tmp_path)),
    )
    pid = int(pid_file.read_text())
    try:
        assert out.is_error and "was killed" in out.text
        assert await _gone(pid), "Bash said 'was killed' but the command still runs"
    finally:
        if _alive(pid):
            os.kill(pid, 9)


async def test_a_cancelled_hook_kills_its_tree(tmp_path) -> None:
    from fluid.hooks.bus import TURN_START
    from fluid.hooks.command import CommandHook

    pid_file = tmp_path / "pid"
    hook = CommandHook(
        event=TURN_START, command=f"sleep 60 & echo $! > {pid_file}; wait",
        cwd=str(tmp_path), timeout=60,
    )
    task = asyncio.create_task(hook(TURN_START, {}))
    pid = int(await _wait_for_file(pid_file))
    try:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await _gone(pid)
    finally:
        if _alive(pid):
            os.kill(pid, 9)


async def test_stopping_a_monitor_kills_a_descendant_of_an_exited_shell(tmp_path) -> None:
    from fluid.cli.host import CliHost, _stop_worker

    pid_file = tmp_path / "pid"
    host = CliHost(None, None)
    receipt = await host.monitor_spawn(
        {"command": f"sleep 60 & echo $! > {pid_file}"}, ToolCtx(cwd=str(tmp_path)),
    )
    worker = host.workers[receipt.task_id]
    pid = int(await _wait_for_file(pid_file))
    try:
        while worker.process.returncode is None:
            await asyncio.sleep(0.02)
        assert _alive(pid)  # the shell is gone, its child holds stdout
        await asyncio.wait_for(_stop_worker(worker), 10)
        assert await _gone(pid)
        assert worker.task.done()
    finally:
        if _alive(pid):
            os.kill(pid, 9)
