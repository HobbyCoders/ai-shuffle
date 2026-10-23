"""A delegated CLI that exits takes the commands it started with it (#436).

The stream-json CLI exits cleanly when its stdin closes and shuts down its
MCP servers itself, but a shell command the model backgrounded (a dev
server, a test run) is still in the CLI's process group. Before #436 that
group was only killed when the CLI ignored the stdin close, so a clean exit
left everything the agent had started running with no owner.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

import pytest

from fluid.engine import delegated
from fluid.engine.delegated import DelegatedLoop
from fluid.tools import priority

posix_only = pytest.mark.skipif(os.name == "nt", reason="process groups are POSIX")
linux_only = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="oom_score_adj is Linux-only",
)


def _alive(pid: int) -> bool:
    """Running, and not a zombie waiting for its reparented parent to reap it."""
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii") as f:
            state = f.read().rsplit(")", 1)[1].split()[0]
        return state != "Z"
    except FileNotFoundError:
        return False
    except OSError:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True


async def _wait_gone(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        await asyncio.sleep(0.05)
    return not _alive(pid)


async def _spawn_cli_that_backgrounds_a_command() -> tuple[asyncio.subprocess.Process, int]:
    """A stand-in CLI: starts a long command in the background, exits on stdin EOF."""
    proc = await asyncio.create_subprocess_exec(
        "sh", "-c", "sleep 120 & echo $!; cat >/dev/null",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    assert proc.stdout is not None
    background = int((await proc.stdout.readline()).decode().strip())
    return proc, background


def _bare_loop(proc) -> DelegatedLoop:
    loop = DelegatedLoop.__new__(DelegatedLoop)
    loop._proc = proc
    loop._stdout_backlog = []
    return loop


@posix_only
async def test_a_clean_cli_exit_ends_the_commands_it_backgrounded() -> None:
    proc, background = await _spawn_cli_that_backgrounds_a_command()
    try:
        assert _alive(background)

        await _bare_loop(proc)._stop_process(grace=5.0)

        assert proc.returncode is not None
        assert await _wait_gone(background), "the backgrounded command outlived its CLI"
    finally:
        try:
            os.kill(background, 9)
        except ProcessLookupError:
            pass


@posix_only
async def test_a_cli_that_already_exited_still_has_its_group_ended() -> None:
    proc, background = await _spawn_cli_that_backgrounds_a_command()
    try:
        assert proc.stdin is not None
        proc.stdin.close()
        await proc.wait()
        assert _alive(background)

        await _bare_loop(proc)._stop_process()

        assert await _wait_gone(background)
    finally:
        try:
            os.kill(background, 9)
        except ProcessLookupError:
            pass


@posix_only
async def test_a_pid_held_by_a_live_process_is_never_signalled() -> None:
    """A live holder of the pid means the old group is gone: hands off."""
    proc = await asyncio.create_subprocess_exec("sleep", "30", start_new_session=True)
    try:
        await delegated._end_process_group(proc.pid, grace=0.2)
        assert proc.returncode is None
        assert _alive(proc.pid)
    finally:
        proc.kill()
        await proc.wait()


async def test_stop_process_ends_the_group_after_a_clean_exit(monkeypatch) -> None:
    """Platform-independent wiring check: the clean path calls the group cleanup."""
    ended: list[int] = []

    async def _record(pid, **_kwargs):
        ended.append(pid)

    monkeypatch.setattr(delegated, "_end_process_group", _record)

    class _Stdin:
        def close(self) -> None:
            pass

    class _Proc:
        pid = 424242
        returncode = None
        stdin = _Stdin()

        async def wait(self) -> int:
            self.returncode = 0
            return 0

    await _bare_loop(_Proc())._stop_process(grace=1.0)

    assert ended == [424242]


async def test_end_process_group_is_a_no_op_for_no_pid() -> None:
    await delegated._end_process_group(None)
    await delegated._end_process_group(0)


# ─── OOM preference for model-run commands ───────────────────────────────────


@linux_only
async def test_raise_oom_score_adj_raises_but_never_lowers() -> None:
    proc = await asyncio.create_subprocess_exec("sleep", "30")
    path = f"/proc/{proc.pid}/oom_score_adj"
    try:
        priority.raise_oom_score_adj(proc.pid, 300)
        with open(path, encoding="ascii") as f:
            assert int(f.read()) == 300
        priority.raise_oom_score_adj(proc.pid, 100)
        with open(path, encoding="ascii") as f:
            assert int(f.read()) == 300
    finally:
        proc.kill()
        await proc.wait()


def test_raise_oom_score_adj_never_raises() -> None:
    priority.raise_oom_score_adj(None)
    priority.raise_oom_score_adj(-1)
    priority.raise_oom_score_adj(2**22 + 12345, 300)  # no such process
    priority.raise_oom_score_adj(os.getpid(), 0)  # off
