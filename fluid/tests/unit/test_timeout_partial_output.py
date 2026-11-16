"""A timed-out command or cell keeps what it printed before the timeout (#1549).

Three paths, each through the real subprocess: Bash's own timeout (covered in
test_tool_output_spill.py), Bash inside a code-mode cell under the kernel's
per-call ceiling, and a code-mode cell past its wall clock. The cell path
interrupts the child with SIGINT, which only POSIX has; on Windows the cell
says its output was lost.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

from fluid.codemode import SubprocessKernel
from fluid.codemode.kernel import HostReply
from fluid.codemode.session import CodeModeSession, _bash_under_ceiling
from fluid.codemode.subprocess_kernel import KernelConfig
from fluid.events import ToolResult
from fluid.permissions.engine import PermissionEngine
from fluid.tools.builtins import bash
from fluid.tools.registry import ToolCtx, ToolRegistry

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="SIGINT interrupt is POSIX-only")


async def _null_host(name: str, kwargs: dict) -> HostReply:  # pragma: no cover
    return HostReply(error="no host")


class _Loop:
    def __init__(self, cwd: str) -> None:
        self.ctx = ToolCtx(cwd=cwd)
        self.hooks = None
        self.permissions = PermissionEngine("never_ask")
        self.events: list = []

    async def _emit(self, event) -> None:
        self.events.append(event)


def _hang_after(text: str) -> str:
    return f"print({text!r}, flush=True)\nwhile True:\n    pass\n"


@posix_only
async def test_a_timed_out_cell_returns_what_it_printed() -> None:
    kernel = SubprocessKernel(KernelConfig())
    await kernel.start()
    try:
        await kernel.exec("keep = 7", timeout=10, on_host_call=_null_host)
        slow = await kernel.exec(_hang_after("FATAL: lock held"), timeout=0.5, on_host_call=_null_host)
        assert "FATAL: lock held" in slow.stdout
        assert slow.error and "wall clock" in slow.error and "kernel restarted" in slow.error
        assert "what it printed before the timeout" in slow.error
        assert "KeyboardInterrupt" not in slow.error  # the interrupt is ours, not the cell's
        # The kernel was still restarted from its checkpoint.
        assert (await kernel.exec("keep", timeout=10, on_host_call=_null_host)).value_repr == "7"
    finally:
        await kernel.stop()


@posix_only
async def test_a_timed_out_cell_keeps_the_stdout_cap_and_spill() -> None:
    """Recovered output goes through the same cap and full-output copy as a
    finished cell's (#1538)."""
    kernel = SubprocessKernel(KernelConfig())
    await kernel.start()
    try:
        code = "print('HEAD')\nprint('x' * 50_000)\nprint('TAIL', flush=True)\nwhile True:\n    pass\n"
        slow = await kernel.exec(code, timeout=0.5, on_host_call=_null_host)
        assert len(slow.stdout) < 20_000 and "HEAD" in slow.stdout and "TAIL" in slow.stdout
        assert "HEAD" in slow.stdout_full and "TAIL" in slow.stdout_full
        assert slow.stdout_total > 50_000
    finally:
        await kernel.stop()


async def test_a_cell_that_swallows_the_interrupt_says_its_output_was_lost() -> None:
    kernel = SubprocessKernel(KernelConfig())
    await kernel.start()
    try:
        code = (
            "print('never seen', flush=True)\n"
            "while True:\n"
            "    try:\n"
            "        while True:\n"
            "            pass\n"
            "    except KeyboardInterrupt:\n"
            "        pass\n"
        )
        slow = await kernel.exec(code, timeout=0.5, on_host_call=_null_host)
        assert slow.stdout == ""
        assert slow.error and "output it printed before the timeout was lost" in slow.error
        assert (await kernel.exec("1 + 1", timeout=10, on_host_call=_null_host)).value_repr == "2"
    finally:
        await kernel.stop()


@posix_only
async def test_a_timed_out_cell_reaches_the_model_through_the_session(tmp_path) -> None:
    session = CodeModeSession(ToolRegistry(), config=KernelConfig())
    try:
        out = await session.run_python(
            sid="s", turn_id="t", loop=_Loop(str(tmp_path)),
            input={"code": _hang_after("FATAL: port in use"), "timeout": 0.5},
        )
    finally:
        await session.aclose()
    assert out.is_error
    assert "FATAL: port in use" in out.text and "printed before the timeout" in out.text


async def test_bash_in_a_cell_times_out_under_the_ceiling_with_its_output(tmp_path) -> None:
    """The kernel cancels a host call at its ceiling, which discards Bash's
    capture. In-kernel Bash runs under a timeout just below that ceiling, so
    its own timeout fires first and the output it printed comes back."""
    registry = ToolRegistry()
    registry.register_builtin(bash.SPEC, bash.handler)
    session = CodeModeSession(registry, config=KernelConfig(host_call_timeout=4))
    loop = _Loop(str(tmp_path))
    command = f'"{sys.executable}" -c "print(\'FATAL: db locked\', flush=True); import time; time.sleep(60)"'
    code = f"from tools.Bash import Bash\nBash(command={command!r}, timeout=60)\n"
    try:
        out = await session.run_python(sid="s", turn_id="t", loop=loop, input={"code": code, "timeout": 2})
    finally:
        await session.aclose()
    results = [e for e in loop.events if isinstance(e, ToolResult) and e.name == "Bash"]
    assert len(results) == 1 and results[0].is_error
    assert "timed out after 1s" in results[0].output  # 4 - kill grace 2 - margin 1
    assert "FATAL: db locked" in results[0].output and "partial output" in results[0].output
    assert "cancelled before it returned" not in results[0].output
    assert "FATAL: db locked" in out.text


def test_bash_under_ceiling_only_lowers_a_timeout_that_would_be_cancelled() -> None:
    def timeout(kwargs: dict, bound: float):
        out, refused = _bash_under_ceiling(kwargs, bound)
        assert refused is None
        return out.get("timeout")

    assert timeout({"command": "x"}, 0) is None  # unbounded: untouched
    assert timeout({"command": "x"}, 900) is None  # default 120 fits
    assert timeout({"command": "x", "timeout": 600}, 900) == 600
    assert timeout({"command": "x"}, 100) == 88  # 100 - grace 2 - margin 10
    assert timeout({"command": "x", "timeout": 10}, 8) == 4  # 8 - 2 - 8/4


@pytest.mark.parametrize("bound, expected", [
    (4.0, 1.0),  # the formula reaches Bash's 1 s floor exactly: 4 - 2 - 1
    (4.4, 4.4 - 2 - 1.1),  # just above it
    (3.9, None),  # just below it: refused, never the 60 s asked for
    (1.0, None),
    (0.001, None),  # the smallest positive ceiling (0 means unbounded)
])
def test_bash_under_ceiling_at_the_floor(bound, expected) -> None:
    """Every ceiling either fits Bash's run plus cleanup under it or refuses
    the call; it never falls back to a timeout larger than the ceiling."""
    out, refused = _bash_under_ceiling({"command": "x", "timeout": 60}, bound)
    if expected is None:
        assert refused and "Bash was not run" in refused and out == {"command": "x", "timeout": 60}
    else:
        assert refused is None and out["timeout"] == pytest.approx(expected)
        assert out["timeout"] + bash.KILL_GRACE_S < bound


async def test_bash_in_a_cell_under_a_too_short_ceiling_is_refused_up_front(tmp_path) -> None:
    registry = ToolRegistry()
    registry.register_builtin(bash.SPEC, bash.handler)
    session = CodeModeSession(registry, config=KernelConfig(host_call_timeout=3))
    loop = _Loop(str(tmp_path))
    marker = tmp_path / "ran"
    code = (
        "from tools.Bash import Bash\n"
        f"Bash(command='echo BEFORE; touch {marker}; sleep 30', timeout=60)\n"
    )
    started = time.monotonic()
    try:
        out = await session.run_python(sid="s", turn_id="t", loop=loop, input={"code": code, "timeout": 1})
    finally:
        await session.aclose()
    assert time.monotonic() - started < 10
    assert out.is_error and "Bash was not run" in out.text and "per-call ceiling is 3s" in out.text
    assert "cancelled before it returned" not in out.text
    assert not marker.exists()
    assert not [e for e in loop.events if isinstance(e, ToolResult) and e.name == "Bash"]


# ─── review round 1 ──────────────────────────────────────────────────────────

def _py(code: str) -> str:
    return f'"{sys.executable}" -c "{code}"'


# Prints, closes both output streams two seconds later, then hangs.
_CLOSES_EARLY = _py(
    "import os, time; print('FATAL: closed early', flush=True); time.sleep(2); "
    "os.close(1); os.close(2); time.sleep(60)"
)


@posix_only
async def test_bash_timeout_bounds_the_whole_run_when_output_closes_early(tmp_path) -> None:
    """S1: the timeout is one deadline for reading and waiting together, not
    a fresh one for the wait after the output closes."""
    started = time.monotonic()
    out = await bash.handler({"command": _CLOSES_EARLY, "timeout": 3}, bash_ctx(tmp_path))
    elapsed = time.monotonic() - started
    assert out.is_error and "timed out after 3s" in out.text
    assert "FATAL: closed early" in out.text
    assert elapsed < 4.5  # a fresh 3 s wait after the close would make it 5 s


@posix_only
async def test_bash_timeout_kills_a_background_child_holding_the_pipe(tmp_path) -> None:
    """The shell exits at once but its background child keeps the pipe open:
    the timeout kills the child too, and cleanup stays inside the grace."""
    pidfile = tmp_path / "child.pid"
    command = f"echo START; sh -c 'echo $$ > {pidfile}; exec sleep 60' & echo END"
    started = time.monotonic()
    out = await bash.handler({"command": command, "timeout": 2}, bash_ctx(tmp_path))
    elapsed = time.monotonic() - started
    assert out.is_error and "START" in out.text and "END" in out.text
    assert elapsed < 3.5  # a drain left waiting on the child would make it 4 s
    assert not _alive(int(pidfile.read_text().strip()))


def _alive(pid: int) -> bool:
    for _ in range(50):
        try:
            state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        except (FileNotFoundError, ProcessLookupError):
            return False
        if state in ("Z", "X"):
            return False
        time.sleep(0.05)
    return True


def bash_ctx(tmp_path) -> ToolCtx:
    return ToolCtx(cwd=str(tmp_path), extra={"harness_state_root": tmp_path / "state"})


@posix_only
async def test_bash_in_a_cell_with_output_closed_early_returns_before_the_ceiling(tmp_path) -> None:
    """S1 through the real session: Bash's total run, kill grace included,
    ends before the kernel's per-call ceiling cancels it."""
    registry = ToolRegistry()
    registry.register_builtin(bash.SPEC, bash.handler)
    session = CodeModeSession(registry, config=KernelConfig(host_call_timeout=8))
    loop = _Loop(str(tmp_path))
    code = f"from tools.Bash import Bash\nBash(command={_CLOSES_EARLY!r}, timeout=60)\n"
    try:
        out = await session.run_python(sid="s", turn_id="t", loop=loop, input={"code": code, "timeout": 2})
    finally:
        await session.aclose()
    results = [e for e in loop.events if isinstance(e, ToolResult) and e.name == "Bash"]
    assert len(results) == 1 and "timed out after 4s" in results[0].output
    assert "FATAL: closed early" in results[0].output
    assert "cancelled before it returned" not in out.text and "FATAL: closed early" in out.text


def _bash_session() -> CodeModeSession:
    registry = ToolRegistry()
    registry.register_builtin(bash.SPEC, bash.handler)
    return CodeModeSession(registry, config=KernelConfig())


async def _run(session: CodeModeSession, loop: _Loop, code: str, timeout: float = 20):
    return await session.run_python(sid="s", turn_id="t", loop=loop, input={"code": code, "timeout": timeout})


async def _started(loop: _Loop, name: str = "Bash") -> None:
    for _ in range(200):
        if any(getattr(e, "name", None) == name and not isinstance(e, ToolResult) for e in loop.events):
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"{name} never started: {loop.events!r}")


_SLOW_CELL = (
    "from tools.Bash import Bash\n"
    "Bash(command='sleep 30')\n"
    "print('RAN AFTER CANCEL')\n"
)


@pytest.mark.parametrize("cancels", [1, 2])
async def test_a_cell_cancelled_during_a_host_call_does_not_run_on(tmp_path, cancels) -> None:
    """S2: the child waits for the cancelled call's reply. Without a restart it
    takes the next cell as that reply and runs the cancelled cell's rest. A
    second cancel lands while the kernel restarts."""
    session = _bash_session()
    loop = _Loop(str(tmp_path))
    try:
        await _run(session, loop, "keep = 7")
        task = asyncio.ensure_future(_run(session, loop, _SLOW_CELL))
        await _started(loop)
        for _ in range(cancels):
            task.cancel()
            await asyncio.sleep(0.05)
        with pytest.raises(asyncio.CancelledError):
            await task
        nxt = await _run(session, loop, "print('NEW CELL', keep)")
    finally:
        await session.aclose()
    assert "NEW CELL 7" in nxt.text
    assert "RAN AFTER CANCEL" not in nxt.text


async def test_a_cancel_during_the_interrupt_grace_resets_the_kernel(tmp_path) -> None:
    """A cell past its deadline that ignores the interrupt keeps the kernel in
    the grace; a cancel there (twice) still leaves a working kernel."""
    session = _bash_session()
    loop = _Loop(str(tmp_path))
    stubborn = (
        "while True:\n"
        "    try:\n"
        "        while True:\n"
        "            pass\n"
        "    except KeyboardInterrupt:\n"
        "        pass\n"
    )
    try:
        await _run(session, loop, "keep = 7")
        task = asyncio.ensure_future(_run(session, loop, stubborn, timeout=0.5))
        await asyncio.sleep(1.2)  # past the deadline, inside the 2 s grace
        task.cancel()
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        nxt = await _run(session, loop, "print('NEW CELL', keep)")
        # And a host call then works on the restarted kernel.
        bash_cell = await _run(session, loop, "from tools.Bash import Bash\nprint(Bash(command='echo OK'))")
    finally:
        await session.aclose()
    assert "NEW CELL 7" in nxt.text
    assert "OK" in bash_cell.text


# ─── review round 2: every restart path is cancel-safe ───────────────────────

@posix_only
@pytest.mark.parametrize("barrier", ["_restore", "_checkpoint_frame"])
async def test_a_cancel_inside_restart_or_checkpoint_restores_on_the_next_cell(barrier) -> None:
    """S4: cancel at the restore of a timeout's restart (the replacement is
    booted, the namespace not yet restored), or inside the checkpoint after a
    cell. The child is killed and forgotten; the next cell restores the
    checkpoint and says so, instead of a live kernel missing ``keep``."""
    kernel = SubprocessKernel(KernelConfig())
    await kernel.start()
    try:
        await kernel.exec("keep = 7", timeout=10, on_host_call=_null_host)
        entered = asyncio.Event()
        real = getattr(kernel, barrier)

        async def blocked(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        setattr(kernel, barrier, blocked)
        code = _hang_after("x") if barrier == "_restore" else "other = 8"
        task = asyncio.ensure_future(kernel.exec(code, timeout=0.5, on_host_call=_null_host))
        await asyncio.wait_for(entered.wait(), timeout=20)
        child = kernel._proc
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        setattr(kernel, barrier, real)
        assert kernel._proc is None
        await asyncio.wait_for(child.wait(), timeout=10)  # killed, not left live
        after = await kernel.exec("keep", timeout=10, on_host_call=_null_host)
        assert after.value_repr == "7"
        assert "namespace restored from last checkpoint" in after.notes
    finally:
        await kernel.stop()


async def test_a_cancel_during_the_first_start_leaves_a_kernel_that_starts_again() -> None:
    kernel = SubprocessKernel(KernelConfig())
    entered = asyncio.Event()
    real = kernel._read_frame

    async def blocked(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    kernel._read_frame = blocked
    task = asyncio.ensure_future(kernel.start())
    await asyncio.wait_for(entered.wait(), timeout=20)
    child = kernel._proc
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    kernel._read_frame = real
    try:
        assert kernel._proc is None
        await asyncio.wait_for(child.wait(), timeout=10)
        assert (await kernel.exec("1 + 1", timeout=10, on_host_call=_null_host)).value_repr == "2"
    finally:
        await kernel.stop()
