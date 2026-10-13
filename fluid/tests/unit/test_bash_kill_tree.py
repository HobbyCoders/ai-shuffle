"""Bash handler: interrupt and timeout must kill the whole process tree.

Regression tests for the leak where cancelling the tool task (the user
pressed Stop mid-command) unwound straight past the running subprocess:
the shell — and on Windows the grandchildren under Git Bash's nested
bash — kept running to completion under the host process.

The probe is a real grandchild: the handler spawns a shell, the shell
spawns a Python that writes its own pid to a file and then sleeps far
longer than the test. If the tree kill works, that pid disappears from
the OS process table; if only the outer shell were killed, it would
survive as an orphan.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from fluid.tools.builtins.bash import handler as bash_handler
from fluid.tools.registry import ToolCtx

_STARTUP_DEADLINE_S = 30.0
_DEATH_DEADLINE_S = 15.0


def _pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        # os.kill(pid, 0) TERMINATES on Windows, so ask tasklist instead.
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
        ).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _probe_command(tmp_path: Path) -> tuple[str, Path]:
    """A shell command whose grandchild records its pid and then sleeps."""
    pid_file = tmp_path / "probe.pid"
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import os, sys, time\n"
        "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
        "time.sleep(120)\n",
        encoding="utf-8",
    )
    return f'"{sys.executable}" "{probe}" "{pid_file}"', pid_file


async def _read_probe_pid(pid_file: Path) -> int:
    deadline = time.monotonic() + _STARTUP_DEADLINE_S
    while time.monotonic() < deadline:
        text = pid_file.read_text(encoding="utf-8") if pid_file.exists() else ""
        if text:
            return int(text)
        await asyncio.sleep(0.05)
    raise AssertionError("probe grandchild never wrote its pid file")


async def _assert_pid_dies(pid: int) -> None:
    deadline = time.monotonic() + _DEATH_DEADLINE_S
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"probe pid {pid} still alive after the kill")


async def test_cancel_kills_process_tree(tmp_path: Path, monkeypatch) -> None:
    created: list[asyncio.subprocess.Process] = []
    real_exec = asyncio.create_subprocess_exec

    async def spy(*args, **kwargs):
        proc = await real_exec(*args, **kwargs)
        created.append(proc)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)

    command, pid_file = _probe_command(tmp_path)
    task = asyncio.create_task(
        bash_handler({"command": command}, ToolCtx(cwd=str(tmp_path)))
    )
    grandchild_pid = await _read_probe_pid(pid_file)
    assert _pid_alive(grandchild_pid)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The shell itself was killed and reaped, not orphaned...
    assert created and created[0].returncode is not None
    # ...and so was the grandchild, which proc.kill() alone would miss.
    await _assert_pid_dies(grandchild_pid)


async def test_timeout_kills_process_tree(tmp_path: Path) -> None:
    command, pid_file = _probe_command(tmp_path)
    task = asyncio.create_task(
        bash_handler(
            {"command": command, "timeout": 6.0}, ToolCtx(cwd=str(tmp_path))
        )
    )
    # The probe starts well inside the 6s window, so its pid is on record
    # before the handler's own timeout fires and kills the tree.
    grandchild_pid = await _read_probe_pid(pid_file)
    assert _pid_alive(grandchild_pid)

    outcome = await task
    assert outcome.is_error
    assert "timed out" in outcome.text
    await _assert_pid_dies(grandchild_pid)
