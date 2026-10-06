"""Process-tree spawn/kill mechanics (audit item 5).

Two failures live here. First, a Monitor shell was killed one process at a
time: on Windows the model's command runs under Git Bash, which nests a second
bash, so terminating the shell left ``python run_local.py`` holding port 8010
while TaskStop reported ``running: false``. Second, a child Python inherited
cp1252 for stdout and died on its first emoji before it ever bound a port.

The mock-driven tests pin the ORDER of the kill (graceful group signal, then
escalation, then a verified sweep of the descendants snapshotted beforehand).
The Windows-marked test at the bottom is the real thing: a genuine
grandchild under a bash wrapper, asserted dead by the OS process table.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core import process_tree as pt

# asyncio_mode = auto (pytest.ini) runs the coroutines below; no marker needed.

_IS_WINDOWS = sys.platform == "win32"


# ---------------------------------------------------------------------------
# spawn_kwargs / child_env
# ---------------------------------------------------------------------------

class TestSpawnKwargs:
    def test_windows_gets_its_own_process_group(self, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(pt, "_IS_WINDOWS", True)
        monkeypatch.setattr(settings, "child_process_nice", 10)
        kwargs = pt.spawn_kwargs()
        assert kwargs == {
            "creationflags": pt._CREATE_NEW_PROCESS_GROUP
            | pt._BELOW_NORMAL_PRIORITY_CLASS
        }
        # Never start_new_session on Windows — the kwarg does not exist there.
        assert "start_new_session" not in kwargs

    def test_windows_priority_off_when_nice_is_zero(self, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(pt, "_IS_WINDOWS", True)
        monkeypatch.setattr(settings, "child_process_nice", 0)
        assert pt.spawn_kwargs() == {"creationflags": pt._CREATE_NEW_PROCESS_GROUP}

    def test_posix_gets_a_new_session(self, monkeypatch):
        monkeypatch.setattr(pt, "_IS_WINDOWS", False)
        assert pt.spawn_kwargs() == {"start_new_session": True}


# ---------------------------------------------------------------------------
# child priority
# ---------------------------------------------------------------------------

class _PriorityRecorder:
    """Stands in for os.getpriority / os.setpriority on any platform."""

    def __init__(self, base: int = 0, error: Exception | None = None):
        self.base = base
        self.error = error
        self.calls: list[tuple[int, int, int]] = []

    def install(self, monkeypatch):
        monkeypatch.setattr(os, "getpriority", lambda which, who: self.base, raising=False)
        monkeypatch.setattr(os, "setpriority", self.setpriority, raising=False)
        return self

    def setpriority(self, which, who, value):
        self.calls.append((which, who, value))
        if self.error is not None:
            raise self.error


class TestChildPriority:
    @pytest.mark.parametrize("value", ["abc", -1, 20, 99, None, True])
    def test_bad_setting_falls_back_to_default(self, monkeypatch, value):
        from app.core.config import settings

        monkeypatch.setattr(settings, "child_process_nice", value)
        assert pt.child_nice() == 10

    def test_lower_priority_sets_the_process_group(self, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "child_process_nice", 7)
        rec = _PriorityRecorder(base=2).install(monkeypatch)
        pt.lower_priority(MagicMock(pid=4321))
        assert rec.calls == [(getattr(os, "PRIO_PGRP", 1), 4321, 9)]

    def test_lower_priority_caps_at_19(self, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "child_process_nice", 10)
        rec = _PriorityRecorder(base=15).install(monkeypatch)
        pt.lower_priority(MagicMock(pid=4321))
        assert rec.calls[0][2] == 19

    def test_lower_priority_off_when_zero(self, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "child_process_nice", 0)
        rec = _PriorityRecorder().install(monkeypatch)
        pt.lower_priority(MagicMock(pid=4321))
        assert rec.calls == []

    @pytest.mark.parametrize("error", [ProcessLookupError(), PermissionError()])
    def test_lower_priority_never_raises(self, monkeypatch, error):
        from app.core.config import settings

        monkeypatch.setattr(settings, "child_process_nice", 10)
        rec = _PriorityRecorder(error=error).install(monkeypatch)
        pt.lower_priority(MagicMock(pid=4321))
        assert len(rec.calls) == 1

    async def test_monitor_spawn_survives_a_failing_setpriority(self, monkeypatch, tmp_path):
        """The Monitor spawn site applies the priority and still runs the command."""
        from app.core.config import settings
        from app.mcp import builtin_tools as bt

        monkeypatch.setattr(settings, "child_process_nice", 10)
        rec = _PriorityRecorder(error=ProcessLookupError()).install(monkeypatch)
        task = await bt._spawn_background(
            f'"{sys.executable}" -c "print(1)"', str(tmp_path), None
        )
        try:
            await asyncio.wait_for(task.process.wait(), timeout=60)
            assert task.process.returncode == 0
            assert [who for _, who, _ in rec.calls] == [task.process.pid]
        finally:
            bt._background_tasks.pop(task.task_id, None)
            if task._reader_task is not None:
                task._reader_task.cancel()

    def test_setting_validator_falls_back_to_default(self):
        from app.core.config import Settings

        validate = Settings._child_process_nice_or_default
        assert validate("5") == 5
        assert validate(0) == 0
        assert validate("abc") == 10
        assert validate("-3") == 10
        assert validate(25) == 10

    def test_startup_copies_the_setting_for_fluid(self, monkeypatch):
        from app.core import startup
        from app.core.config import settings
        from fluid.tools.priority import CHILD_NICE_ENV, child_nice

        monkeypatch.setenv(CHILD_NICE_ENV, "3")  # restored after the test
        monkeypatch.setattr(settings, "child_process_nice", 0)
        startup.propagate_child_priority()
        assert os.environ[CHILD_NICE_ENV] == "0"
        assert child_nice() == 0


class TestChildEnv:
    def test_windows_forces_utf8_io(self, monkeypatch):
        monkeypatch.setattr(pt, "_IS_WINDOWS", True)
        monkeypatch.delenv("PYTHONIOENCODING", raising=False)
        monkeypatch.delenv("PYTHONUTF8", raising=False)
        env = pt.child_env()
        assert env["PYTHONIOENCODING"] == "utf-8"
        assert env["PYTHONUTF8"] == "1"

    def test_operator_setting_wins(self, monkeypatch):
        monkeypatch.setattr(pt, "_IS_WINDOWS", True)
        monkeypatch.setenv("PYTHONIOENCODING", "latin-1")
        assert pt.child_env()["PYTHONIOENCODING"] == "latin-1"

    def test_posix_left_alone(self, monkeypatch):
        monkeypatch.setattr(pt, "_IS_WINDOWS", False)
        monkeypatch.delenv("PYTHONIOENCODING", raising=False)
        assert "PYTHONIOENCODING" not in pt.child_env()

    def test_extra_overrides(self, monkeypatch):
        monkeypatch.setattr(pt, "_IS_WINDOWS", False)
        assert pt.child_env({"FOO": "bar"})["FOO"] == "bar"


# ---------------------------------------------------------------------------
# kill_process_tree
# ---------------------------------------------------------------------------

def _fake_proc(pid: int = 4242, *, exits_on_terminate: bool = True):
    proc = MagicMock()
    proc.pid = pid
    proc.returncode = None

    def _terminate():
        if exits_on_terminate:
            proc.returncode = 0

    proc.terminate = MagicMock(side_effect=_terminate)
    proc.kill = MagicMock(side_effect=lambda: setattr(proc, "returncode", -9))

    async def _wait():
        if proc.returncode is None:
            await asyncio.sleep(10)  # never returns inside the grace window
        return proc.returncode

    proc.wait = AsyncMock(side_effect=_wait)
    return proc


class TestKillProcessTree:
    async def test_graceful_group_signal_then_reap(self, monkeypatch):
        monkeypatch.setattr(pt, "_IS_WINDOWS", False)
        proc = _fake_proc()
        calls = []

        def _group(pid, *, force, own_group):
            calls.append((pid, force, own_group))
            proc.returncode = 0
            return True

        monkeypatch.setattr(pt, "_signal_group", _group)
        monkeypatch.setattr(pt, "descendant_pids", lambda pid: [])

        survivors = await pt.kill_process_tree(proc, own_process_group=True)

        assert survivors == []
        assert calls == [(4242, False, True)]
        # The group already took it; no need for the single-process ask.
        proc.terminate.assert_not_called()
        proc.kill.assert_not_called()

    async def test_escalates_to_force_when_grace_expires(self, monkeypatch):
        monkeypatch.setattr(pt, "_IS_WINDOWS", False)
        proc = _fake_proc(exits_on_terminate=False)
        calls = []

        def _group(pid, *, force, own_group):
            calls.append(force)
            if force:
                proc.returncode = -9
            return True

        monkeypatch.setattr(pt, "_signal_group", _group)
        monkeypatch.setattr(pt, "descendant_pids", lambda pid: [])

        survivors = await pt.kill_process_tree(
            proc, own_process_group=True, grace=0.05
        )

        assert survivors == []
        assert calls == [False, True]
        proc.kill.assert_called_once()

    async def test_no_group_falls_back_to_terminate(self, monkeypatch):
        """A process we did not put in its own group must never be signalled by
        group — killpg/CTRL_BREAK there would hit the SERVER."""
        monkeypatch.setattr(pt, "_IS_WINDOWS", False)
        proc = _fake_proc()
        monkeypatch.setattr(pt, "descendant_pids", lambda pid: [])

        with patch.object(pt, "os") as fake_os:
            survivors = await pt.kill_process_tree(proc, own_process_group=False)

        assert survivors == []
        fake_os.killpg.assert_not_called()
        proc.terminate.assert_called_once()

    async def test_snapshot_is_taken_before_the_kill(self, monkeypatch):
        """Descendants must be enumerated while the parent links still exist."""
        monkeypatch.setattr(pt, "_IS_WINDOWS", False)
        proc = _fake_proc()
        order = []

        def _descendants(pid):
            order.append("snapshot")
            return [777]

        def _group(pid, *, force, own_group):
            order.append("kill")
            proc.returncode = 0
            return True

        monkeypatch.setattr(pt, "descendant_pids", _descendants)
        monkeypatch.setattr(pt, "_signal_group", _group)
        monkeypatch.setattr(pt, "pid_alive", lambda pid: False)

        await pt.kill_process_tree(proc, own_process_group=True)
        assert order == ["snapshot", "kill"]

    async def test_orphaned_descendant_is_force_killed(self, monkeypatch):
        """The Windows shape: the shell dies, its grandchild does not."""
        monkeypatch.setattr(pt, "_IS_WINDOWS", True)
        monkeypatch.setattr(pt, "_ORPHAN_SETTLE_S", 0.0)
        proc = _fake_proc()
        alive = {9001: True}
        killed = []

        monkeypatch.setattr(pt, "descendant_pids", lambda pid: [9001])
        monkeypatch.setattr(pt, "pid_alive", lambda pid: alive.get(pid, False))

        def _force(pid):
            killed.append(pid)
            alive[pid] = False

        monkeypatch.setattr(pt, "_force_kill_pid", _force)
        monkeypatch.setattr(
            pt, "_signal_group",
            lambda pid, *, force, own_group: setattr(proc, "returncode", 0) or True,
        )

        survivors = await pt.kill_process_tree(proc, own_process_group=True)

        assert killed == [9001]
        assert survivors == []

    async def test_unkillable_descendant_is_reported_not_hidden(self, monkeypatch):
        """The honesty contract: what we could not kill comes back named."""
        monkeypatch.setattr(pt, "_IS_WINDOWS", True)
        monkeypatch.setattr(pt, "_ORPHAN_SETTLE_S", 0.0)
        proc = _fake_proc()
        monkeypatch.setattr(pt, "descendant_pids", lambda pid: [9001, 9002])
        monkeypatch.setattr(pt, "pid_alive", lambda pid: pid == 9002)
        monkeypatch.setattr(pt, "_force_kill_pid", lambda pid: None)
        monkeypatch.setattr(
            pt, "_signal_group",
            lambda pid, *, force, own_group: setattr(proc, "returncode", 0) or True,
        )

        survivors = await pt.kill_process_tree(
            proc, own_process_group=True, verify=0.05
        )
        assert survivors == [9002]

    async def test_already_exited_process_still_sweeps(self, monkeypatch):
        proc = _fake_proc()
        proc.returncode = 0
        monkeypatch.setattr(pt, "_ORPHAN_SETTLE_S", 0.0)
        monkeypatch.setattr(pt, "descendant_pids", lambda pid: [123])
        monkeypatch.setattr(pt, "pid_alive", lambda pid: False)

        assert await pt.kill_process_tree(proc) == []
        proc.terminate.assert_not_called()

    async def test_none_process_is_a_noop(self):
        assert await pt.kill_process_tree(None) == []


class TestSurvivorWarning:
    def test_names_pids_and_ports(self, monkeypatch):
        monkeypatch.setattr(pt, "listening_ports", lambda pids: [8010])
        text = pt.survivor_warning("4f2a1b", [91234])
        assert "4f2a1b" in text
        assert "91234" in text
        assert "8010" in text

    def test_without_ports_still_names_pids(self, monkeypatch):
        monkeypatch.setattr(pt, "listening_ports", lambda pids: [])
        text = pt.survivor_warning("abc", [1, 2])
        assert "1, 2" in text


# ---------------------------------------------------------------------------
# The real thing (Windows)
# ---------------------------------------------------------------------------

def _pid_alive_os(pid: int) -> bool:
    """Ask the OS directly — deliberately NOT the module under test."""
    if _IS_WINDOWS:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True,
        ).stdout
        return str(pid) in (out or "")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def _await_pid_file(pid_file: Path, timeout: float = 30.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pid_file.exists():
            text = pid_file.read_text(encoding="utf-8").strip()
            if text:
                return int(text)
        await asyncio.sleep(0.05)
    raise AssertionError("probe grandchild never wrote its pid file")


@pytest.mark.skipif(not _IS_WINDOWS, reason="Windows process-tree behaviour")
async def test_windows_tree_kill_leaves_no_survivor(tmp_path):
    """A real grandchild under a bash wrapper must not outlive the stop.

    This is the audited bug end to end: Git Bash nests a second bash, so the
    sleeping Python here sits two levels below the process the harness holds a
    handle to. Killing that handle alone leaves it running — which is how a
    stopped dev server kept port 8010 bound.
    """
    from app.mcp.builtin_tools import _spawn_background, stop_background_shell_task

    probe = tmp_path / "probe.py"
    pid_file = tmp_path / "probe.pid"
    probe.write_text(
        "import os, sys, time\n"
        "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    command = f'"{sys.executable}" "{probe}" "{pid_file}"'

    task = await _spawn_background(command, str(tmp_path), None)
    try:
        assert task.own_process_group is True
        grandchild = await _await_pid_file(pid_file)
        assert _pid_alive_os(grandchild)

        result = await stop_background_shell_task(task.task_id)
        assert result is not None
        assert not result.get("survivors"), result.get("warning")
        assert result["running"] is False
        assert not _pid_alive_os(grandchild)
    finally:
        from app.mcp import builtin_tools as bt

        bt._background_tasks.pop(task.task_id, None)
        if task._reader_task is not None:
            task._reader_task.cancel()


@pytest.mark.skipif(not _IS_WINDOWS, reason="Windows child encoding")
async def test_windows_child_python_can_print_non_cp1252(tmp_path):
    """The instantly-crashing launch: an emoji on stdout must not kill the child."""
    from app.mcp.builtin_tools import _spawn_background

    script = tmp_path / "emoji.py"
    script.write_text("print('launched \\N{ROCKET}')\n", encoding="utf-8")
    task = await _spawn_background(
        f'"{sys.executable}" "{script}"', str(tmp_path), None
    )
    try:
        await asyncio.wait_for(task.process.wait(), timeout=60)
        assert task.process.returncode == 0
        # Give the reader a beat to drain the pipe it is still buffering.
        for _ in range(100):
            if task.lines:
                break
            await asyncio.sleep(0.05)
        assert any("launched" in line for line in task.lines)
    finally:
        from app.mcp import builtin_tools as bt

        bt._background_tasks.pop(task.task_id, None)


_PRIORITY_PROBE = (
    "import os, sys\n"
    "if sys.platform == 'win32':\n"
    "    import ctypes\n"
    "    k = ctypes.windll.kernel32\n"
    "    k.GetCurrentProcess.restype = ctypes.c_void_p\n"
    "    k.GetPriorityClass.argtypes = [ctypes.c_void_p]\n"
    "    print('class', hex(k.GetPriorityClass(k.GetCurrentProcess())))\n"
    "else:\n"
    "    print('nice', os.getpriority(os.PRIO_PROCESS, 0))\n"
)


async def test_monitor_command_runs_below_the_server(monkeypatch, tmp_path):
    """A real Monitor shell: the Python it starts reports its own priority.

    The probe is a grandchild (shell, then Python), so this also checks that
    the priority reaches what the shell forks.
    """
    from app.core.config import settings
    from app.mcp import builtin_tools as bt

    monkeypatch.setattr(settings, "child_process_nice", 10)
    script = tmp_path / "prio.py"
    script.write_text(_PRIORITY_PROBE, encoding="utf-8")
    task = await bt._spawn_background(
        f'sleep 0.2; "{sys.executable}" "{script}"', str(tmp_path), None
    )
    try:
        await asyncio.wait_for(task.process.wait(), timeout=60)
        for _ in range(100):
            if any(line.startswith(("class", "nice")) for line in task.lines):
                break
            await asyncio.sleep(0.05)
        reported = next(
            line.strip() for line in task.lines if line.startswith(("class", "nice"))
        )
        if _IS_WINDOWS:
            assert reported == f"class {hex(pt._BELOW_NORMAL_PRIORITY_CLASS)}"
        else:
            expected = min(19, os.getpriority(os.PRIO_PROCESS, 0) + 10)
            assert reported == f"nice {expected}"
    finally:
        bt._background_tasks.pop(task.task_id, None)
        if task._reader_task is not None:
            task._reader_task.cancel()
